"""LocalEngine: Whisper transcription, sentence segmentation, Claude virality scoring, and 9:16 rendering."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.engines.base import (
    ClipEngine,
    ClipEngineError,
    ClipResult,
    ClipSettings,
)
from app.services.engines.stub import probe_video_duration
from app.services.metadata import build_clip_title

logger = logging.getLogger(__name__)

SCORING_SYSTEM_PROMPT = """You are an expert viral short-form video editor for YouTube Shorts and TikTok.
Analyze the provided candidate video clips and score each from 0 to 100 based on virality, engagement, and retention.

Evaluation criteria:
1. Hook strength in the first 3 seconds (captures immediate attention).
2. Complete idea with a clear payoff (no abrupt start or cliffhanger without resolution).
3. Understandable without prior context (standalone value).
4. High emotional or informational value.

Respond ONLY with a valid JSON array of objects. Do NOT include markdown code fences or conversational filler.
Format:
[
  {"id": 0, "score": 88, "reason": "Compelling opening hook with an insightful standalone conclusion."},
  {"id": 1, "score": 42, "reason": "Requires too much background context to understand the discussion."}
]
"""


@dataclass
class WordToken:
    """A single transcribed word with timestamp bounds."""

    word: str
    start: float
    end: float
    probability: float = 1.0


@dataclass
class Sentence:
    """A sentence-bounded sequence of words with start and end timestamps."""

    start_sec: float
    end_sec: float
    text: str
    words: list[WordToken] = field(default_factory=list)


@dataclass
class CandidateSegment:
    """A candidate clip segment composed of contiguous complete sentences."""

    id: int
    start_sec: float
    end_sec: float
    text: str
    words: list[WordToken] = field(default_factory=list)
    score: float = 0.0
    reason: str = ""
    is_heuristic: bool = False


def probe_video_dimensions(source_path: Path) -> tuple[int, int]:
    """Return video (width, height) using ffprobe."""
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(source_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        raw = proc.stdout.strip()
        parts = raw.split("x")
        if len(parts) >= 2:
            return int(parts[0]), int(parts[1])
    except Exception as exc:
        logger.warning("Could not probe video dimensions for %s: %s", source_path.name, exc)
    return 1920, 1080


def extract_audio(source_path: Path, output_audio_path: Path) -> Path:
    """Extract audio track as 16 kHz mono 16-bit WAV for Whisper."""
    output_audio_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(output_audio_path),
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        raise ClipEngineError(f"Audio extraction failed: {exc.stderr[-400:]}") from exc
    return output_audio_path


def format_ass_time(seconds: float) -> str:
    """Format seconds into ASS subtitle timestamp format: H:MM:SS.cs."""
    secs = max(0.0, float(seconds))
    hours = int(secs // 3600)
    minutes = int((secs % 3600) // 60)
    remaining_secs = secs % 60
    centis = int(round((remaining_secs - int(remaining_secs)) * 100))
    if centis >= 100:
        centis = 99
    return f"{hours}:{minutes:02d}:{int(remaining_secs):02d}.{centis:02d}"


def extract_sentences_from_transcript(transcript_data: dict[str, Any]) -> list[Sentence]:
    """Parse transcript data into complete sentences bounded by punctuation."""
    sentences: list[Sentence] = []
    current_words: list[WordToken] = []

    segments = transcript_data.get("segments", [])
    for seg in segments:
        raw_words = seg.get("words", [])
        if not raw_words:
            # Segment has text but no word timestamps
            seg_text = str(seg.get("text", "")).strip()
            if seg_text:
                seg_start = float(seg.get("start", 0.0))
                seg_end = float(seg.get("end", seg_start + 1.0))
                sentences.append(
                    Sentence(
                        start_sec=seg_start,
                        end_sec=seg_end,
                        text=seg_text,
                        words=[],
                    )
                )
            continue

        for w in raw_words:
            word_str = str(w.get("word", "")).strip()
            if not word_str:
                continue
            token = WordToken(
                word=word_str,
                start=float(w.get("start", 0.0)),
                end=float(w.get("end", 0.0)),
                probability=float(w.get("probability", 1.0)),
            )
            current_words.append(token)
            # Check for sentence terminal punctuation
            if word_str[-1] in ".!?":
                sent_text = " ".join(t.word for t in current_words)
                sentences.append(
                    Sentence(
                        start_sec=current_words[0].start,
                        end_sec=current_words[-1].end,
                        text=sent_text,
                        words=list(current_words),
                    )
                )
                current_words.clear()

    if current_words:
        sent_text = " ".join(t.word for t in current_words)
        sentences.append(
            Sentence(
                start_sec=current_words[0].start,
                end_sec=current_words[-1].end,
                text=sent_text,
                words=list(current_words),
            )
        )

    return sentences


def generate_candidate_segments(
    sentences: list[Sentence],
    min_duration: float = 20.0,
    max_duration: float = 60.0,
) -> list[CandidateSegment]:
    """Generate sentence-bounded candidate segments within [min_duration, max_duration].

    Guarantees:
      - Segments start and end strictly on sentence boundaries.
      - Never cuts mid-word.
    """
    candidates: list[CandidateSegment] = []
    if not sentences:
        return candidates

    cand_id = 0
    num_sentences = len(sentences)

    for i in range(num_sentences):
        accumulated_text: list[str] = []
        accumulated_words: list[WordToken] = []
        start_sec = sentences[i].start_sec

        for j in range(i, num_sentences):
            end_sec = sentences[j].end_sec
            duration = end_sec - start_sec

            accumulated_text.append(sentences[j].text)
            accumulated_words.extend(sentences[j].words)

            if min_duration <= duration <= max_duration:
                candidates.append(
                    CandidateSegment(
                        id=cand_id,
                        start_sec=round(start_sec, 2),
                        end_sec=round(end_sec, 2),
                        text=" ".join(accumulated_text),
                        words=list(accumulated_words),
                    )
                )
                cand_id += 1
            elif duration > max_duration:
                # If first sentence alone exceeded max_duration, keep it as standalone candidate
                if j == i:
                    candidates.append(
                        CandidateSegment(
                            id=cand_id,
                            start_sec=round(start_sec, 2),
                            end_sec=round(end_sec, 2),
                            text=" ".join(accumulated_text),
                            words=list(accumulated_words),
                        )
                    )
                    cand_id += 1
                break

    # If transcript is very short and no window reached min_duration, take all sentences
    if not candidates and sentences:
        start_sec = sentences[0].start_sec
        end_sec = sentences[-1].end_sec
        all_text = " ".join(s.text for s in sentences)
        all_words = [w for s in sentences for w in s.words]
        candidates.append(
            CandidateSegment(
                id=0,
                start_sec=round(start_sec, 2),
                end_sec=round(end_sec, 2),
                text=all_text,
                words=all_words,
            )
        )

    return candidates


def compute_heuristic_score(candidate: CandidateSegment) -> tuple[float, str]:
    """Evaluate candidate virality heuristically based on speech density and structure."""
    duration = max(1.0, candidate.end_sec - candidate.start_sec)
    words = candidate.text.split()
    word_count = len(words)
    words_per_sec = word_count / duration

    # 1. Speech density: optimal short-form pacing is ~2.2 - 3.4 words/second (up to 40 pts)
    if 2.2 <= words_per_sec <= 3.4:
        density_score = 40.0
    elif 1.6 <= words_per_sec < 2.2 or 3.4 < words_per_sec <= 4.2:
        density_score = 30.0
    elif words_per_sec > 0.8:
        density_score = 20.0
    else:
        density_score = 10.0

    # 2. Sentence completeness & punctuation (up to 35 pts)
    completeness_score = 10.0
    clean_text = candidate.text.strip()
    if clean_text and clean_text[-1] in ".!?":
        completeness_score += 15.0
    sentence_count = len(re.findall(r"[.!?]+(?:\s|$)", clean_text))
    if sentence_count >= 2:
        completeness_score += 10.0

    # 3. Duration suitability: 25 - 45s is the sweet spot for Shorts (up to 25 pts)
    if 25.0 <= duration <= 45.0:
        duration_score = 25.0
    elif 20.0 <= duration <= 60.0:
        duration_score = 18.0
    else:
        duration_score = 10.0

    total_score = round(
        min(100.0, max(15.0, density_score + completeness_score + duration_score)),
        1,
    )
    reason = "[Heuristic] High speech density and complete sentence structure"
    return total_score, reason


def parse_claude_json_response(raw_text: str) -> list[dict[str, Any]]:
    """Safely parse Claude's response into a JSON array, handling markdown fences and wraps."""
    clean = raw_text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    clean = clean.strip()

    try:
        data = json.loads(clean)
        if isinstance(data, list):
            return data
    except Exception:
        pass

    # Regex search for [...] block
    match = re.search(r"\[\s*\{.*?\}\s*\]", clean, re.DOTALL)
    if match:
        data = json.loads(match.group(0))
        if isinstance(data, list):
            return data

    raise ValueError("Failed to parse valid JSON array from Claude response.")


def score_candidates_with_claude(
    candidates: list[CandidateSegment],
    api_key: str,
    model: str = "claude-sonnet-5-5",
    batch_size: int = 8,
) -> None:
    """Score candidate clips using Anthropic Claude Messages API with safe retry and fallback.

    CRITICAL PRIVACY & COST RULE: Never logs transcript text or API keys.
    """
    if not api_key.strip():
        logger.info("ANTHROPIC_API_KEY is not set. Falling back to heuristic scoring.")
        for cand in candidates:
            cand.score, cand.reason = compute_heuristic_score(cand)
            cand.is_heuristic = True
        return

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:
        logger.warning(
            "Could not initialize Anthropic client (%s). Using heuristic scoring.",
            type(exc).__name__,
        )
        for cand in candidates:
            cand.score, cand.reason = compute_heuristic_score(cand)
            cand.is_heuristic = True
        return

    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        items_payload = [
            {
                "id": cand.id,
                "duration_sec": round(cand.end_sec - cand.start_sec, 2),
                "transcript": cand.text,
            }
            for cand in batch
        ]

        user_content = (
            f"Evaluate these candidate video clips:\n{json.dumps(items_payload, indent=2)}"
        )
        parsed_scores: dict[int, dict[str, Any]] = {}

        try:
            # Turn 1: Primary prompt
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                temperature=0.2,
                system=SCORING_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
            raw_text = response.content[0].text if response.content else ""
            parsed = parse_claude_json_response(raw_text)
            for item in parsed:
                if "id" in item and "score" in item:
                    parsed_scores[int(item["id"])] = item
        except Exception as first_exc:
            logger.warning(
                "Claude scoring turn 1 encountered %s. Retrying once.",
                type(first_exc).__name__,
            )
            # Turn 2: Retry with correction request
            try:
                retry_response = client.messages.create(
                    model=model,
                    max_tokens=2048,
                    temperature=0.0,
                    system=SCORING_SYSTEM_PROMPT,
                    messages=[
                        {"role": "user", "content": user_content},
                        {
                            "role": "assistant",
                            "content": raw_text if "raw_text" in locals() else "",
                        },
                        {
                            "role": "user",
                            "content": (
                                "Your previous response was not valid JSON. "
                                "Return ONLY a valid JSON array of objects matching "
                                '[{"id": ..., "score": ..., "reason": "..."}].'
                            ),
                        },
                    ],
                )
                retry_text = (
                    retry_response.content[0].text if retry_response.content else ""
                )
                parsed = parse_claude_json_response(retry_text)
                for item in parsed:
                    if "id" in item and "score" in item:
                        parsed_scores[int(item["id"])] = item
            except Exception as retry_exc:
                logger.warning(
                    "Claude scoring retry failed with %s. Falling back to heuristic scoring.",
                    type(retry_exc).__name__,
                )

        # Apply parsed scores or fallback
        for cand in batch:
            if cand.id in parsed_scores:
                item = parsed_scores[cand.id]
                cand.score = float(item.get("score", 50.0))
                cand.reason = str(
                    item.get("reason", "Selected by AI content evaluation.")
                ).strip()
                cand.is_heuristic = False
            else:
                cand.score, cand.reason = compute_heuristic_score(cand)
                cand.is_heuristic = True


def token_jaccard_similarity(text1: str, text2: str) -> float:
    """Compute Jaccard token overlap between two candidate transcripts."""
    stopwords = {
        "the", "a", "an", "and", "or", "in", "on", "at", "to", "for", "of",
        "with", "is", "it", "this", "that", "you", "i", "we", "they", "was",
    }
    tokens1 = {
        w.lower() for w in re.findall(r"\w+", text1) if w.lower() not in stopwords
    }
    tokens2 = {
        w.lower() for w in re.findall(r"\w+", text2) if w.lower() not in stopwords
    }
    if not tokens1 or not tokens2:
        return 0.0
    intersection = len(tokens1 & tokens2)
    union = len(tokens1 | tokens2)
    return intersection / union if union > 0 else 0.0


def select_top_candidates(
    candidates: list[CandidateSegment],
    clip_count: int,
    min_gap_sec: float = 3.0,
    diversity_threshold: float = 0.45,
) -> list[CandidateSegment]:
    """Greedily select top N scoring candidates enforcing non-overlap, min gap, and diversity."""
    if not candidates:
        return []

    sorted_cands = sorted(candidates, key=lambda c: c.score, reverse=True)
    selected: list[CandidateSegment] = []

    def is_compatible(cand: CandidateSegment, check_diversity: bool) -> bool:
        for s in selected:
            # Enforce non-overlapping with minimum gap
            if not (
                cand.end_sec + min_gap_sec <= s.start_sec
                or cand.start_sec >= s.end_sec + min_gap_sec
            ):
                return False
            # Enforce topic diversity
            if (
                check_diversity
                and token_jaccard_similarity(cand.text, s.text) > diversity_threshold
            ):
                return False
        return True

    # Pass 1: Strict non-overlap, min gap, and topic diversity
    for cand in sorted_cands:
        if is_compatible(cand, check_diversity=True):
            selected.append(cand)
            if len(selected) >= clip_count:
                break

    # Pass 2: If more clips requested, relax diversity while strictly keeping non-overlap & gap
    if len(selected) < clip_count:
        for cand in sorted_cands:
            if cand not in selected and is_compatible(cand, check_diversity=False):
                selected.append(cand)
                if len(selected) >= clip_count:
                    break

    # Return ordered chronologically by start timestamp
    return sorted(selected, key=lambda c: c.start_sec)


def generate_ass_subtitles(
    words: list[WordToken],
    clip_start_sec: float,
    clip_end_sec: float,
    output_ass_path: Path,
) -> bool:
    """Generate an ASS subtitle file chunking 2-4 words at a time with clear readable styling."""
    valid_words = [
        w
        for w in words
        if w.start >= clip_start_sec - 0.5 and w.end <= clip_end_sec + 0.5
    ]
    if not valid_words:
        return False

    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,40,40,280,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    chunk_size = 3
    for i in range(0, len(valid_words), chunk_size):
        chunk = valid_words[i : i + chunk_size]
        start_rel = max(0.0, chunk[0].start - clip_start_sec)
        end_rel = max(start_rel + 0.35, chunk[-1].end - clip_start_sec)
        text = " ".join(w.word for w in chunk).strip()
        if text:
            # Escape ASS special chars
            safe_text = text.replace("{", "\\{").replace("}", "\\}")
            events.append(
                f"Dialogue: 0,{format_ass_time(start_rel)},{format_ass_time(end_rel)},Default,,0,0,0,,{safe_text}"
            )

    if not events:
        return False

    output_ass_path.parent.mkdir(parents=True, exist_ok=True)
    output_ass_path.write_text(ass_header + "\n".join(events) + "\n", encoding="utf-8")
    return True


def render_vertical_clip(
    source_path: Path,
    output_file: Path,
    start_sec: float,
    end_sec: float,
    source_width: int,
    source_height: int,
    blurred_background: bool,
    ass_path: Path | None,
) -> None:
    """Render 1080x1920 vertical clip with loudness normalization and burned-in captions."""
    clip_duration = max(0.5, end_sec - start_sec)
    is_horizontal = source_width > source_height

    escaped_ass = (
        str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\\\''")
        if ass_path and ass_path.is_file()
        else None
    )

    if is_horizontal and blurred_background:
        # Blurred background fit for horizontal source video
        filter_complex = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:5[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
        if escaped_ass:
            filter_complex += f",ass='{escaped_ass}'"
        vf_args = ["-filter_complex", filter_complex]
    else:
        # Default: 9:16 center-crop
        vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
        if escaped_ass:
            vf += f",ass='{escaped_ass}'"
        vf_args = ["-vf", vf]

    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        f"{start_sec:.2f}",
        "-i",
        str(source_path),
        "-t",
        f"{clip_duration:.2f}",
        *vf_args,
        "-af",
        "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(output_file),
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as exc:
        # If subtitles filter failed (e.g. ffmpeg missing libass), retry once cleanly without ASS
        if escaped_ass:
            logger.warning(
                "ffmpeg rendering with ASS subtitles failed; retrying without subtitles."
            )
            render_vertical_clip(
                source_path=source_path,
                output_file=output_file,
                start_sec=start_sec,
                end_sec=end_sec,
                source_width=source_width,
                source_height=source_height,
                blurred_background=blurred_background,
                ass_path=None,
            )
            return
        raise ClipEngineError(
            f"ffmpeg failed rendering clip ({start_sec:.2f}s - {end_sec:.2f}s): {exc.stderr[-400:]}"
        ) from exc


class LocalEngine(ClipEngine):
    """Production local AI clipping engine with Whisper transcription and Claude scoring."""

    name: str = "local"

    def transcribe(
        self,
        audio_path: Path,
        cache_path: Path,
        max_transcript_chars: int,
        whisper_model_name: str = "small",
    ) -> dict[str, Any]:
        """Transcribe audio track via faster-whisper with caching and cost/privacy guards."""
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if "segments" in cached:
                    logger.info("Using cached transcript from %s", cache_path.name)
                    return cached
            except Exception as exc:
                logger.warning("Failed reading cached transcript: %s", exc)

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ClipEngineError(
                "faster-whisper is not installed. Please install faster-whisper or use Docker worker."
            ) from exc

        logger.info(
            "Transcribing audio with faster-whisper (model=%s, device=cpu, int8)...",
            whisper_model_name,
        )
        model = WhisperModel(whisper_model_name, device="cpu", compute_type="int8")
        segments_raw, info = model.transcribe(
            str(audio_path),
            word_timestamps=True,
            beam_size=5,
        )

        segments_list: list[dict[str, Any]] = []
        full_text_parts: list[str] = []

        for seg in segments_raw:
            text = seg.text.strip()
            full_text_parts.append(text)
            words_data: list[dict[str, Any]] = []
            if hasattr(seg, "words") and seg.words:
                for w in seg.words:
                    words_data.append(
                        {
                            "word": w.word,
                            "start": round(w.start, 2),
                            "end": round(w.end, 2),
                            "probability": round(w.probability, 3),
                        }
                    )
            segments_list.append(
                {
                    "id": seg.id,
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": text,
                    "words": words_data,
                }
            )

        full_text = " ".join(full_text_parts)

        # Cost & Privacy Guard: Enforce maximum transcript length
        if len(full_text) > max_transcript_chars:
            raise ClipEngineError(
                f"Transcript length ({len(full_text)} characters) exceeds "
                f"maximum allowed limit ({max_transcript_chars} characters)."
            )

        transcript_data = {
            "duration": getattr(info, "duration", 0.0),
            "language": getattr(info, "language", "en"),
            "text": full_text,
            "segments": segments_list,
        }

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(transcript_data, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("Could not cache transcript to disk: %s", exc)

        return transcript_data

    def create_clips(
        self,
        source_path: Path,
        settings: ClipSettings,
    ) -> list[ClipResult]:
        """Execute full clipping pipeline: transcription, scoring, selection, rendering."""
        if not source_path.is_file():
            raise ClipEngineError(f"Source video file does not exist: {source_path}")

        app_settings = get_settings()
        settings.output_dir.mkdir(parents=True, exist_ok=True)
        job_dir = settings.output_dir.parent

        # 1. Probe source video duration & dimensions
        total_duration = probe_video_duration(source_path)
        source_width, source_height = probe_video_dimensions(source_path)

        # 2. Extract audio and transcribe
        audio_path = job_dir / "audio_16k.wav"
        transcript_cache_path = job_dir / "transcript.json"

        if not transcript_cache_path.is_file():
            extract_audio(source_path, audio_path)

        transcript_data = self.transcribe(
            audio_path=audio_path,
            cache_path=transcript_cache_path,
            max_transcript_chars=app_settings.max_transcript_chars,
            whisper_model_name=app_settings.whisper_model,
        )

        # 3. Sentence-bounded candidate generation
        sentences = extract_sentences_from_transcript(transcript_data)
        min_dur = float(getattr(settings, "min_clip_duration_sec", 20))
        max_dur = float(getattr(settings, "max_clip_duration_sec", 60))

        candidates = generate_candidate_segments(
            sentences=sentences,
            min_duration=min_dur,
            max_duration=max_dur,
        )

        if not candidates:
            # Fallback if no speech detected in video
            logger.warning(
                "No speech candidates found in transcript; generating duration-based fallback segment."
            )
            fallback_duration = min(max_dur, total_duration)
            candidates = [
                CandidateSegment(
                    id=0,
                    start_sec=0.0,
                    end_sec=fallback_duration,
                    text=settings.source_title or "Highlight clip",
                    words=[],
                )
            ]

        # 4. Score candidates (Claude API or heuristic fallback)
        score_candidates_with_claude(
            candidates=candidates,
            api_key=app_settings.anthropic_api_key,
            model=app_settings.anthropic_model,
        )

        # 5. Select top N non-overlapping, diverse candidates
        selected = select_top_candidates(
            candidates=candidates,
            clip_count=settings.clip_count,
            min_gap_sec=3.0,
            diversity_threshold=0.45,
        )

        if not selected:
            raise ClipEngineError("Failed to select any valid clip candidates.")

        # 6. Render selected clips with ffmpeg (9:16 crop, loudness norm, burned ASS subtitles)
        results: list[ClipResult] = []
        total_clips = len(selected)

        for idx, cand in enumerate(selected, start=1):
            clip_file = settings.output_dir / f"clip_{idx:02d}.mp4"
            ass_file = settings.output_dir / f"clip_{idx:02d}_subs.ass"

            # Generate ASS subtitles if word tokens exist
            has_subs = generate_ass_subtitles(
                words=cand.words,
                clip_start_sec=cand.start_sec,
                clip_end_sec=cand.end_sec,
                output_ass_path=ass_file,
            )

            render_vertical_clip(
                source_path=source_path,
                output_file=clip_file,
                start_sec=cand.start_sec,
                end_sec=cand.end_sec,
                source_width=source_width,
                source_height=source_height,
                blurred_background=settings.blurred_background,
                ass_path=ass_file if has_subs else None,
            )

            # Metadata tags
            metadata: dict[str, Any] = {
                "scorer": "heuristic" if cand.is_heuristic else "claude",
                "speech_density_wps": round(
                    len(cand.words) / max(1.0, cand.end_sec - cand.start_sec), 2
                ),
            }
            if cand.is_heuristic:
                metadata["tags"] = ["heuristic"]

            excerpt = cand.text[:250].strip()
            if len(cand.text) > 250:
                excerpt += "…"

            results.append(
                ClipResult(
                    file_path=clip_file,
                    start_sec=cand.start_sec,
                    end_sec=cand.end_sec,
                    score=cand.score,
                    title=build_clip_title(settings.source_title, idx, total_clips),
                    reason=cand.reason,
                    transcript_excerpt=excerpt,
                    metadata_json=metadata,
                )
            )

        return results
