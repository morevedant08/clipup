"""Tests for LocalEngine: Whisper transcription, candidate segmentation, Claude virality scoring, and selection."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.services.engines.base import ClipEngineError, ClipSettings
from app.services.engines.local import (
    CandidateSegment,
    LocalEngine,
    Sentence,
    WordToken,
    compute_heuristic_score,
    extract_sentences_from_transcript,
    format_ass_time,
    generate_ass_subtitles,
    generate_candidate_segments,
    parse_claude_json_response,
    score_candidates_with_claude,
    select_top_candidates,
    token_jaccard_similarity,
)


def _build_dummy_transcript(num_sentences: int = 5, words_per_sentence: int = 15) -> dict:
    """Build structured mock transcript data with word timestamps."""
    segments = []
    current_time = 0.0
    full_text_list = []

    for s_idx in range(num_sentences):
        words = []
        sent_words = []
        for w_idx in range(words_per_sentence):
            word_str = f"word{s_idx}_{w_idx}"
            if w_idx == words_per_sentence - 1:
                word_str += "."
            sent_words.append(word_str)
            words.append({
                "word": word_str,
                "start": round(current_time, 2),
                "end": round(current_time + 0.5, 2),
                "probability": 0.95,
            })
            current_time += 0.6

        sent_text = " ".join(sent_words)
        full_text_list.append(sent_text)
        segments.append({
            "id": s_idx,
            "start": words[0]["start"],
            "end": words[-1]["end"],
            "text": sent_text,
            "words": words,
        })

    return {
        "duration": current_time,
        "language": "en",
        "text": " ".join(full_text_list),
        "segments": segments,
    }


def test_format_ass_time() -> None:
    """Test formatting seconds into ASS H:MM:SS.cs timestamp format."""
    assert format_ass_time(0.0) == "0:00:00.00"
    assert format_ass_time(5.5) == "0:00:05.50"
    assert format_ass_time(65.25) == "0:01:05.25"
    assert format_ass_time(3661.12) == "1:01:01.12"


def test_extract_sentences_from_transcript() -> None:
    """Test extracting clean sentences bounded by terminal punctuation (.!?)."""
    transcript = _build_dummy_transcript(num_sentences=3, words_per_sentence=5)
    sentences = extract_sentences_from_transcript(transcript)

    assert len(sentences) == 3
    for s in sentences:
        assert s.text.endswith(".")
        assert s.start_sec < s.end_sec
        assert len(s.words) == 5
        # Start and end match word boundaries exactly
        assert s.start_sec == s.words[0].start
        assert s.end_sec == s.words[-1].end


def test_generate_candidates_bounded_by_sentences_never_mid_word() -> None:
    """Candidate segments must strictly start and end on sentence boundaries within duration bounds."""
    transcript = _build_dummy_transcript(num_sentences=6, words_per_sentence=10)
    # Each sentence is 10 words * 0.6s = ~6 seconds
    sentences = extract_sentences_from_transcript(transcript)

    candidates = generate_candidate_segments(
        sentences,
        min_duration=10.0,
        max_duration=25.0,
    )

    assert len(candidates) > 0
    for cand in candidates:
        duration = cand.end_sec - cand.start_sec
        assert 10.0 <= duration <= 25.0
        # Text must end with a full sentence's punctuation
        assert cand.text[-1] in ".!?"
        # Starts on first word of some sentence, ends on last word of some sentence
        assert cand.start_sec == cand.words[0].start
        assert cand.end_sec == cand.words[-1].end


def test_generate_candidates_short_transcript_fallback() -> None:
    """Short transcript that does not reach min_duration should fall back to entire text."""
    transcript = _build_dummy_transcript(num_sentences=1, words_per_sentence=4)
    sentences = extract_sentences_from_transcript(transcript)

    candidates = generate_candidate_segments(
        sentences,
        min_duration=30.0,
        max_duration=60.0,
    )
    assert len(candidates) == 1
    assert candidates[0].text == sentences[0].text
    assert candidates[0].start_sec == sentences[0].start_sec
    assert candidates[0].end_sec == sentences[0].end_sec


def test_parse_claude_json_response() -> None:
    """Test robust JSON parsing from raw Claude responses including markdown code fences."""
    # 1. Plain JSON array
    plain = '[{"id": 0, "score": 85, "reason": "Strong hook"}]'
    parsed = parse_claude_json_response(plain)
    assert len(parsed) == 1
    assert parsed[0]["score"] == 85

    # 2. Markdown fenced JSON
    fenced = """```json
[
  {"id": 1, "score": 92, "reason": "Engaging intro and clear payoff."}
]
```"""
    parsed_fenced = parse_claude_json_response(fenced)
    assert len(parsed_fenced) == 1
    assert parsed_fenced[0]["id"] == 1
    assert parsed_fenced[0]["score"] == 92

    # 3. Conversational wrapper text around JSON
    wrapped = """Here is the virality scoring for the clips:
[
  {"id": 2, "score": 78, "reason": "Informative concept."}
]
Hope this helps!"""
    parsed_wrapped = parse_claude_json_response(wrapped)
    assert len(parsed_wrapped) == 1
    assert parsed_wrapped[0]["id"] == 2

    # 4. Completely invalid text raises ValueError
    with pytest.raises(ValueError, match="Failed to parse valid JSON array"):
        parse_claude_json_response("This is not JSON at all.")


def test_compute_heuristic_score() -> None:
    """Test heuristic scoring calculation based on speech density and structure."""
    cand = CandidateSegment(
        id=0,
        start_sec=0.0,
        end_sec=30.0,
        text="This is a fantastic lesson on programming. It covers all the essential details completely.",
        words=[WordToken(word=f"w{i}", start=i * 0.4, end=i * 0.4 + 0.3) for i in range(75)],
    )
    score, reason = compute_heuristic_score(cand)
    assert 20.0 <= score <= 100.0
    assert "[Heuristic]" in reason


def test_score_candidates_fallback_when_api_key_empty() -> None:
    """When ANTHROPIC_API_KEY is empty, fallback to heuristic scorer with heuristic metadata."""
    candidates = [
        CandidateSegment(
            id=0,
            start_sec=0.0,
            end_sec=25.0,
            text="First candidate clip with high density.",
            words=[],
        ),
        CandidateSegment(
            id=1,
            start_sec=30.0,
            end_sec=55.0,
            text="Second candidate clip with another topic.",
            words=[],
        ),
    ]

    score_candidates_with_claude(candidates, api_key="")

    for cand in candidates:
        assert cand.is_heuristic is True
        assert cand.score > 0
        assert "[Heuristic]" in cand.reason


def test_score_candidates_with_mocked_claude_api() -> None:
    """Test scoring candidates via Anthropic API with successful mocked response."""
    candidates = [
        CandidateSegment(id=0, start_sec=0.0, end_sec=25.0, text="First topic.", words=[]),
        CandidateSegment(id=1, start_sec=30.0, end_sec=55.0, text="Second topic.", words=[]),
    ]

    mock_client = MagicMock()
    mock_msg = MagicMock()
    mock_msg.content = [
        MagicMock(text=json.dumps([
            {"id": 0, "score": 95, "reason": "Viral hook in first 3 seconds."},
            {"id": 1, "score": 60, "reason": "Good but lacks strong conclusion."},
        ]))
    ]
    mock_client.messages.create.return_value = mock_msg

    with patch("anthropic.Anthropic", return_value=mock_client):
        score_candidates_with_claude(candidates, api_key="sk-ant-test-key")

    assert candidates[0].score == 95.0
    assert candidates[0].reason == "Viral hook in first 3 seconds."
    assert candidates[0].is_heuristic is False
    assert candidates[1].score == 60.0
    assert candidates[1].is_heuristic is False


def test_score_candidates_retries_on_malformed_json_and_falls_back_on_error() -> None:
    """Claude scoring should retry on malformed JSON; if retry fails, fall back to heuristic."""
    candidates = [
        CandidateSegment(id=0, start_sec=0.0, end_sec=25.0, text="Candidate text.", words=[]),
    ]

    mock_client = MagicMock()
    # Turn 1: returns malformed output
    bad_msg = MagicMock(content=[MagicMock(text="Sorry, I cannot return JSON.")])
    # Turn 2: still malformed output
    bad_retry_msg = MagicMock(content=[MagicMock(text="Still invalid JSON.")])
    mock_client.messages.create.side_effect = [bad_msg, bad_retry_msg]

    with patch("anthropic.Anthropic", return_value=mock_client):
        score_candidates_with_claude(candidates, api_key="sk-ant-test-key")

    assert mock_client.messages.create.call_count == 2
    # Should safely fall back to heuristic scoring without raising
    assert candidates[0].is_heuristic is True
    assert "[Heuristic]" in candidates[0].reason


def test_select_top_candidates_non_overlapping_and_min_gap() -> None:
    """Selected clips must not overlap and must satisfy minimum gap between clips."""
    cands = [
        # Candidate 0 (high score, 0-20s)
        CandidateSegment(id=0, start_sec=0.0, end_sec=20.0, text="Alpha topic overview.", score=95.0),
        # Candidate 1 (overlapping with cand 0: 10-30s)
        CandidateSegment(id=1, start_sec=10.0, end_sec=30.0, text="Alpha topic detail.", score=90.0),
        # Candidate 2 (too close to cand 0: 21-40s, gap is only 1s < 3s)
        CandidateSegment(id=2, start_sec=21.0, end_sec=40.0, text="Beta topic intro.", score=85.0),
        # Candidate 3 (valid non-overlap and gap: 25-45s, gap is 5s)
        CandidateSegment(id=3, start_sec=25.0, end_sec=45.0, text="Gamma topic discussion.", score=80.0),
    ]

    selected = select_top_candidates(cands, clip_count=2, min_gap_sec=3.0)

    assert len(selected) == 2
    assert selected[0].id == 0
    assert selected[1].id == 3
    # Check gap
    assert selected[1].start_sec >= selected[0].end_sec + 3.0


def test_select_top_candidates_diversity_filter() -> None:
    """Candidates discussing the near-identical topic (high token Jaccard) are filtered for diversity."""
    cands = [
        CandidateSegment(
            id=0,
            start_sec=0.0,
            end_sec=20.0,
            text="Quantum computing revolutionize cryptography RSA security encryption.",
            score=95.0,
        ),
        CandidateSegment(
            id=1,
            start_sec=30.0,
            end_sec=50.0,
            text="Quantum computing revolutionizes cryptography RSA encryption algorithms.",
            score=92.0,  # Near-identical topic to cand 0
        ),
        CandidateSegment(
            id=2,
            start_sec=60.0,
            end_sec=80.0,
            text="Gardening tomatoes soil sunlight watering hydroponics harvest.",
            score=75.0,  # Distinct topic
        ),
    ]

    selected = select_top_candidates(cands, clip_count=2, min_gap_sec=3.0, diversity_threshold=0.4)

    assert len(selected) == 2
    # Should pick cand 0 (score 95) and cand 2 (distinct topic), skipping cand 1
    assert selected[0].id == 0
    assert selected[1].id == 2


def test_generate_ass_subtitles(tmp_path: Path) -> None:
    """Test generating readable ASS subtitle file with 2-4 words highlighted."""
    words = [
        WordToken(word="Welcome", start=10.0, end=10.5),
        WordToken(word="to", start=10.5, end=10.7),
        WordToken(word="Clipper", start=10.7, end=11.2),
        WordToken(word="Shorts", start=11.3, end=11.8),
    ]
    out_ass = tmp_path / "test.ass"
    success = generate_ass_subtitles(words, clip_start_sec=10.0, clip_end_sec=15.0, output_ass_path=out_ass)

    assert success is True
    assert out_ass.is_file()
    content = out_ass.read_text(encoding="utf-8")
    assert "[Script Info]" in content
    assert "[V4+ Styles]" in content
    assert "[Events]" in content
    assert "Dialogue: 0," in content
    assert "Welcome to Clipper" in content


def test_cost_guard_exceeds_max_transcript_chars(tmp_path: Path) -> None:
    """Cost guard must abort with clear error message when transcript length exceeds max_transcript_chars."""
    engine = LocalEngine()

    fake_cache = tmp_path / "cache.json"
    audio_wav = tmp_path / "audio.wav"
    audio_wav.touch()

    # Mock WhisperModel transcribe returning huge transcript
    mock_model = MagicMock()
    mock_seg = MagicMock()
    mock_seg.id = 0
    mock_seg.start = 0.0
    mock_seg.end = 100.0
    mock_seg.text = "A" * 5000  # 5,000 chars
    mock_seg.words = []
    mock_info = MagicMock(duration=100.0, language="en")
    mock_model.transcribe.return_value = ([mock_seg], mock_info)

    mock_module = MagicMock()
    mock_module.WhisperModel.return_value = mock_model

    with patch.dict("sys.modules", {"faster_whisper": mock_module}):
        with pytest.raises(ClipEngineError) as exc_info:
            engine.transcribe(
                audio_path=audio_wav,
                cache_path=fake_cache,
                max_transcript_chars=2000,  # Limit is 2,000
            )

    assert "exceeds maximum allowed limit" in str(exc_info.value)


def test_local_engine_end_to_end_with_synthetic_video(tmp_path: Path) -> None:
    """End-to-end test: LocalEngine produces 1080x1920 clips with score, reason, and transcript excerpt."""
    source_video = tmp_path / "source.mp4"
    # Create a 10-second synthetic 1280x720 video with test audio tone using ffmpeg
    import subprocess
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000",
        "-t", "10",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-c:a", "aac",
        str(source_video),
    ]
    subprocess.run(cmd, check=True, capture_output=True)

    job_clips_dir = tmp_path / "clips"
    job_dir = tmp_path

    # Provide cached transcript so faster-whisper model download is not triggered during unit tests
    transcript_cache = job_dir / "transcript.json"
    dummy_transcript = _build_dummy_transcript(num_sentences=4, words_per_sentence=6)
    transcript_cache.write_text(json.dumps(dummy_transcript), encoding="utf-8")

    engine = LocalEngine()
    settings = ClipSettings(
        output_dir=job_clips_dir,
        clip_count=2,
        min_clip_duration_sec=3,
        max_clip_duration_sec=7,
        source_title="Test Video Title",
    )

    results = engine.create_clips(source_video, settings)

    assert len(results) > 0
    for res in results:
        assert res.file_path.is_file()
        assert res.score > 0
        assert res.reason is not None and len(res.reason) > 0
        assert res.transcript_excerpt is not None
        assert res.metadata_json.get("scorer") in ("claude", "heuristic")

        # Verify output video resolution is 1080x1920
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0:s=x",
            str(res.file_path),
        ]
        probe_res = subprocess.run(probe_cmd, check=True, capture_output=True, text=True)
        assert probe_res.stdout.strip() == "1080x1920"
