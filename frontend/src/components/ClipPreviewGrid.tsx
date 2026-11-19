"use client";

import { useState } from "react";
import {
  buildMediaUrl,
  queueClipUpload,
  shouldSuggestFileUpload,
  updateClipReview,
} from "@/lib/api";
import type {
  ClipItem,
  JobItem,
  ReviewStatus,
  UploadItem,
  UploadPrivacy,
} from "@/lib/types";
import {
  ReviewBadge,
  RightsBasisBadge,
  StatusBadge,
  UploadStatusBadge,
  VerificationLevelBadge,
} from "./Badges";

interface ClipPreviewGridProps {
  job: JobItem;
  uploadEnabled: boolean;
  onClipUpdated: (jobId: string, updatedClip: ClipItem) => void;
  onUploadQueued: (jobId: string, clipId: string, upload: UploadItem) => void;
}

interface UploadDraft {
  title: string;
  description: string;
  tagsRaw: string;
  privacy: UploadPrivacy;
  scheduledAt: string;
  selfAttestedConfirmed: boolean;
}

function formatSeconds(sec: number): string {
  const mins = Math.floor(sec / 60);
  const rem = Math.floor(sec % 60);
  return `${mins}:${rem.toString().padStart(2, "0")}`;
}

export function ClipPreviewGrid({
  job,
  uploadEnabled,
  onClipUpdated,
  onUploadQueued,
}: ClipPreviewGridProps) {
  const isSelfAttested =
    job.verification_level === "self_attested" ||
    ((job.rights_evidence_json as Record<string, unknown> | undefined)
      ?.verification_level === "self_attested");

  const [updatingId, setUpdatingId] = useState<string | null>(null);
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState<"score" | "time">("score");
  const [openUploadClipId, setOpenUploadClipId] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, UploadDraft>>({});
  const [uploadingClipId, setUploadingClipId] = useState<string | null>(null);
  const [uploadErrors, setUploadErrors] = useState<Record<string, string>>({});

  async function handleReview(clip: ClipItem, decision: ReviewStatus) {
    setUpdatingId(clip.id);
    setReviewError(null);
    try {
      const updated = await updateClipReview(job.id, clip.id, decision);
      onClipUpdated(job.id, updated);
      if (decision !== "approved" && openUploadClipId === clip.id) {
        setOpenUploadClipId(null);
      }
    } catch (err) {
      setReviewError(
        err instanceof Error ? err.message : "Failed to update clip review."
      );
    } finally {
      setUpdatingId(null);
    }
  }

  function openUploadEditor(clip: ClipItem) {
    setDrafts((prev) => {
      if (prev[clip.id]) return prev;
      return {
        ...prev,
        [clip.id]: {
          title: clip.title || job.source_title || "Clipper Short",
          description: "",
          tagsRaw: "Shorts",
          privacy: "private",
          scheduledAt: "",
          selfAttestedConfirmed: false,
        },
      };
    });
    setOpenUploadClipId((curr) => (curr === clip.id ? null : clip.id));
  }

  function updateDraft(clipId: string, patch: Partial<UploadDraft>) {
    setDrafts((prev) => ({
      ...prev,
      [clipId]: {
        ...(prev[clipId] || {
          title: "",
          description: "",
          tagsRaw: "",
          privacy: "private",
          scheduledAt: "",
          selfAttestedConfirmed: false,
        }),
        ...patch,
      },
    }));
  }

  async function handleSubmitUpload(e: React.FormEvent, clip: ClipItem) {
    e.preventDefault();
    const draft = drafts[clip.id];
    if (!draft) return;

    setUploadingClipId(clip.id);
    setUploadErrors((prev) => {
      const next = { ...prev };
      delete next[clip.id];
      return next;
    });

    try {
      const tags = draft.tagsRaw
        .split(",")
        .map((t) => t.trim())
        .filter(Boolean);
      const scheduledIso = draft.scheduledAt
        ? new Date(draft.scheduledAt).toISOString()
        : null;

      const created = await queueClipUpload(job.id, clip.id, {
        title: draft.title.trim(),
        description: draft.description,
        tags,
        privacy: draft.privacy,
        scheduled_at: scheduledIso,
        self_attested_confirmed: draft.selfAttestedConfirmed,
      });
      onUploadQueued(job.id, clip.id, created);
      setOpenUploadClipId(null);
    } catch (err) {
      setUploadErrors((prev) => ({
        ...prev,
        [clip.id]:
          err instanceof Error ? err.message : "Failed to queue YouTube upload.",
      }));
    } finally {
      setUploadingClipId(null);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/70 p-6">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 className="text-base font-semibold text-white">
            {job.source_title || `YouTube Video (${job.youtube_video_id})`}
          </h3>
          <a
            href={job.source_url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-indigo-400 hover:underline"
          >
            {job.source_url}
          </a>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {job.source_mode === "uploaded_file" && (
            <span className="inline-flex items-center rounded-full border border-slate-700 bg-slate-800/80 px-2.5 py-0.5 text-xs font-medium text-slate-200">
              Uploaded Source{job.uploaded_filename ? `: ${job.uploaded_filename}` : ""}
            </span>
          )}
          <StatusBadge status={job.status} />
          <RightsBasisBadge basis={job.rights_basis} />
          <VerificationLevelBadge
            level={
              job.verification_level ||
              ((job.rights_evidence_json as Record<string, unknown> | undefined)
                ?.verification_level as string | undefined)
            }
          />
        </div>
      </div>

      {job.status === "rejected" && job.rejection_reason && (
        <div className="mb-4 rounded-xl border border-rose-800/80 bg-rose-950/60 p-4 text-xs leading-relaxed text-rose-200">
          <p className="mb-1 font-semibold text-rose-100">
            Rights Gate Rejection Reason
          </p>
          <p>{job.rejection_reason}</p>
        </div>
      )}

      {job.status === "failed" && job.error_message && (
        <div className="mb-4 rounded-xl border border-red-800/80 bg-red-950/60 p-4 text-xs text-red-200">
          <p className="mb-1 font-semibold text-red-100">Processing Failed</p>
          <p className="leading-relaxed">
            {job.error_message.length > 240
              ? `${job.error_message.slice(0, 237)}...`
              : job.error_message}
          </p>
          {shouldSuggestFileUpload(job.error_message) && (
            <p className="mt-2 rounded-lg border border-amber-800/70 bg-amber-950/50 px-3 py-2 text-amber-200">
              YouTube blocked automated downloading for this video. You can still process it by pasting the same YouTube URL and selecting{" "}
              <span className="font-semibold">
                Upload source file instead of downloading
              </span>{" "}
              in the New Clipping Job form above.
            </p>
          )}
          <p className="mt-1.5 text-[11px] text-red-300/75">
            Check worker logs for full diagnostic details.
          </p>
        </div>
      )}

      {reviewError && (
        <div className="mb-4 rounded-lg border border-rose-800 bg-rose-950/60 px-3 py-2 text-xs text-rose-200">
          {reviewError}
        </div>
      )}

      {job.clips.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 bg-slate-950/50 p-8 text-center text-sm text-slate-400">
          {job.status === "queued" || job.status === "processing"
            ? "Clips are being generated by the worker. This view updates automatically."
            : job.status === "rejected"
              ? "No clips were generated because this video did not pass the Rights Gate."
              : "No clips available for this job."}
        </div>
      ) : (
        <>
          <div className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-950/60 px-4 py-2.5">
            <span className="text-xs font-medium text-slate-400">
              {job.clips.length} clip{job.clips.length === 1 ? "" : "s"} generated
            </span>
            <div className="flex items-center gap-2">
              <span className="text-xs text-slate-400">Sort by:</span>
              <button
                type="button"
                onClick={() => setSortBy("score")}
                className={`rounded-lg px-2.5 py-1 text-xs font-medium transition ${
                  sortBy === "score"
                    ? "bg-indigo-600 text-white"
                    : "bg-slate-800 text-slate-400 hover:text-white"
                }`}
              >
                Highest Score
              </button>
              <button
                type="button"
                onClick={() => setSortBy("time")}
                className={`rounded-lg px-2.5 py-1 text-xs font-medium transition ${
                  sortBy === "time"
                    ? "bg-indigo-600 text-white"
                    : "bg-slate-800 text-slate-400 hover:text-white"
                }`}
              >
                Chronological
              </button>
            </div>
          </div>

          <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {[...job.clips]
              .sort((a, b) => {
                if (sortBy === "score") {
                  const scoreA = a.score ?? 0;
                  const scoreB = b.score ?? 0;
                  if (scoreB !== scoreA) {
                    return scoreB - scoreA;
                  }
                }
                return a.start_sec - b.start_sec;
              })
              .map((clip) => {
                const videoUrl = buildMediaUrl(clip.preview_url);
                const latestUpload = clip.latest_upload || null;
                const isUploadEditorOpen = openUploadClipId === clip.id;
                const draft = drafts[clip.id];
                const clipUploadError = uploadErrors[clip.id];
                const isHeuristic =
                  (clip.metadata_json as Record<string, unknown> | undefined)
                    ?.scorer === "heuristic" ||
                  (
                    (clip.metadata_json as Record<string, unknown> | undefined)
                      ?.tags as string[] | undefined
                  )?.includes("heuristic");

                return (
                  <div
                    key={clip.id}
                    className="flex flex-col overflow-hidden rounded-xl border border-slate-800 bg-slate-950"
                  >
                    <div className="relative aspect-[9/16] w-full bg-black">
                      {videoUrl ? (
                        <video
                          src={videoUrl}
                          controls
                          preload="metadata"
                          className="h-full w-full object-contain"
                        />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-slate-500">
                          Preview unavailable
                        </div>
                      )}
                    </div>

                    <div className="flex flex-1 flex-col justify-between p-4">
                      <div>
                        <div className="flex items-start justify-between gap-2">
                          <h4 className="line-clamp-2 text-sm font-medium text-white">
                            {clip.title || "Vertical Clip"}
                          </h4>
                          <ReviewBadge status={clip.review_status} />
                        </div>

                        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
                          <span>
                            {formatSeconds(clip.start_sec)} – {formatSeconds(clip.end_sec)}
                          </span>
                          {clip.score !== null && (
                            <span className="inline-flex items-center gap-1.5 rounded-md border border-indigo-700/60 bg-indigo-950/70 px-2 py-0.5 font-semibold text-indigo-300">
                              <span>
                                Score: {clip.score > 1 ? Math.round(clip.score) : Math.round(clip.score * 100)}/100
                              </span>
                              {isHeuristic && (
                                <span className="rounded bg-indigo-900/80 px-1 py-0.2 text-[9px] uppercase tracking-wider text-indigo-200">
                                  Heuristic
                                </span>
                              )}
                            </span>
                          )}
                        </div>

                        {clip.reason && (
                          <div className="mt-2.5 rounded-lg border border-purple-800/50 bg-purple-950/40 p-2 text-[11px] leading-relaxed text-purple-200">
                            <span className="font-semibold text-purple-300">Virality Analysis: </span>
                            {clip.reason}
                          </div>
                        )}

                        {clip.transcript_excerpt && (
                          <div className="mt-2 rounded-lg border border-slate-800/80 bg-slate-900/50 p-2 text-[11px] leading-relaxed text-slate-300">
                            <span className="font-semibold text-slate-400">Excerpt: </span>
                            <span className="italic">"{clip.transcript_excerpt}"</span>
                          </div>
                        )}

                        {clip.attribution_text && (
                          <div className="mt-3 rounded-lg border border-teal-800/70 bg-teal-950/40 p-2 text-[11px] leading-relaxed text-teal-200">
                            <span className="font-semibold">CC Attribution: </span>
                            {clip.attribution_text}
                          </div>
                        )}

                    {latestUpload && (
                      <div className="mt-3 space-y-1.5 rounded-lg border border-slate-800 bg-slate-900/70 p-2.5 text-xs">
                        <div className="flex flex-wrap items-center justify-between gap-1.5">
                          <UploadStatusBadge
                            status={latestUpload.status}
                            scheduledAt={latestUpload.scheduled_at}
                          />
                          <span className="text-[11px] capitalize text-slate-400">
                            {latestUpload.privacy}
                          </span>
                        </div>
                        {latestUpload.scheduled_at &&
                          latestUpload.status === "queued" && (
                            <p className="text-[11px] text-amber-200">
                              Scheduled:{" "}
                              {new Date(latestUpload.scheduled_at).toLocaleString()}
                            </p>
                          )}
                        {latestUpload.status === "uploaded" &&
                          latestUpload.youtube_url && (
                            <a
                              href={latestUpload.youtube_url}
                              target="_blank"
                              rel="noreferrer"
                              className="inline-block text-[11px] font-medium text-emerald-300 hover:underline"
                            >
                              View Short on YouTube →
                            </a>
                          )}
                        {latestUpload.status === "failed" && latestUpload.error && (
                          <p className="text-[11px] text-rose-300">
                            {latestUpload.error}
                          </p>
                        )}
                      </div>
                    )}
                  </div>

                  <div className="mt-4 space-y-2.5">
                    <div className="grid grid-cols-2 gap-2">
                      <button
                        type="button"
                        disabled={updatingId === clip.id}
                        onClick={() => handleReview(clip, "approved")}
                        className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                          clip.review_status === "approved"
                            ? "bg-emerald-600 text-white"
                            : "border border-emerald-800/70 bg-emerald-950/50 text-emerald-300 hover:bg-emerald-900/60"
                        }`}
                      >
                        Approve
                      </button>
                      <button
                        type="button"
                        disabled={updatingId === clip.id}
                        onClick={() => handleReview(clip, "rejected")}
                        className={`rounded-lg px-3 py-1.5 text-xs font-medium transition ${
                          clip.review_status === "rejected"
                            ? "bg-rose-600 text-white"
                            : "border border-rose-800/70 bg-rose-950/50 text-rose-300 hover:bg-rose-900/60"
                        }`}
                      >
                        Reject
                      </button>
                    </div>

                    {clip.review_status === "approved" ? (
                      <div>
                        <button
                          type="button"
                          onClick={() => openUploadEditor(clip)}
                          className="w-full rounded-lg bg-indigo-600 px-3 py-2 text-xs font-semibold text-white transition hover:bg-indigo-500"
                        >
                          {isUploadEditorOpen
                            ? "Cancel Upload"
                            : latestUpload?.status === "uploaded"
                              ? "Upload Again to YouTube"
                              : "Upload to YouTube"}
                        </button>

                        {isUploadEditorOpen && draft && (
                          <form
                            onSubmit={(e) => handleSubmitUpload(e, clip)}
                            className="mt-3 space-y-2.5 rounded-xl border border-slate-800 bg-slate-900/80 p-3"
                          >
                            {!uploadEnabled && (
                              <p className="rounded-md border border-amber-800/80 bg-amber-950/60 p-2 text-[11px] text-amber-200">
                                Click <strong>Enable uploading</strong> in the top bar first to grant YouTube upload permission.
                              </p>
                            )}

                            <div>
                              <label className="mb-1 block text-[11px] text-slate-300">
                                Title (max 100 chars)
                              </label>
                              <input
                                type="text"
                                required
                                maxLength={100}
                                value={draft.title}
                                onChange={(e) =>
                                  updateDraft(clip.id, { title: e.target.value })
                                }
                                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                              />
                            </div>

                            <div>
                              <label className="mb-1 block text-[11px] text-slate-300">
                                Description
                              </label>
                              <textarea
                                rows={2}
                                value={draft.description}
                                onChange={(e) =>
                                  updateDraft(clip.id, {
                                    description: e.target.value,
                                  })
                                }
                                placeholder="Add your own description..."
                                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                              />
                              <p className="mt-1 text-[10px] text-slate-400">
                                #Shorts
                                {job.rights_basis === "cc" && " + CC attribution"}
                                {job.campaign_id && " + campaign hashtags"} added
                                automatically.
                              </p>
                            </div>

                            <div>
                              <label className="mb-1 block text-[11px] text-slate-300">
                                Tags (comma-separated)
                              </label>
                              <input
                                type="text"
                                value={draft.tagsRaw}
                                onChange={(e) =>
                                  updateDraft(clip.id, { tagsRaw: e.target.value })
                                }
                                placeholder="Shorts, podcast, highlights"
                                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2.5 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                              />
                            </div>

                            <div className="grid grid-cols-2 gap-2">
                              <div>
                                <label className="mb-1 block text-[11px] text-slate-300">
                                  Privacy
                                </label>
                                <select
                                  value={draft.privacy}
                                  onChange={(e) =>
                                    updateDraft(clip.id, {
                                      privacy: e.target.value as UploadPrivacy,
                                    })
                                  }
                                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                                >
                                  <option value="private">Private</option>
                                  <option value="unlisted">Unlisted</option>
                                  <option value="public">Public</option>
                                </select>
                              </div>

                              <div>
                                <label className="mb-1 block text-[11px] text-slate-300">
                                  Schedule (optional)
                                </label>
                                <input
                                  type="datetime-local"
                                  value={draft.scheduledAt}
                                  onChange={(e) =>
                                    updateDraft(clip.id, {
                                      scheduledAt: e.target.value,
                                    })
                                  }
                                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-2 py-1.5 text-[11px] text-white focus:border-indigo-500 focus:outline-none"
                                />
                              </div>
                            </div>

                            {isSelfAttested && (
                              <div className="rounded-lg border border-amber-800/80 bg-amber-950/40 p-2.5">
                                <label className="flex items-start gap-2 text-[11px] text-amber-200 cursor-pointer">
                                  <input
                                    type="checkbox"
                                    required
                                    checked={draft.selfAttestedConfirmed || false}
                                    onChange={(e) =>
                                      updateDraft(clip.id, {
                                        selfAttestedConfirmed: e.target.checked,
                                      })
                                    }
                                    className="mt-0.5 h-3.5 w-3.5 rounded border-slate-700 bg-slate-900 text-amber-600 focus:ring-amber-500"
                                  />
                                  <span>
                                    <strong>Rights Confirmation:</strong> I confirm and attest that I hold valid clipping rights for this video under this self-attested basis ({job.rights_basis}).
                                  </span>
                                </label>
                              </div>
                            )}

                            {clipUploadError && (
                              <p className="rounded-md border border-rose-800/80 bg-rose-950/60 p-2 text-[11px] text-rose-200">
                                {clipUploadError}
                              </p>
                            )}

                            <button
                              type="submit"
                              disabled={
                                !uploadEnabled ||
                                uploadingClipId === clip.id ||
                                (isSelfAttested && !draft.selfAttestedConfirmed)
                              }
                              className="w-full rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white transition hover:bg-emerald-500 disabled:opacity-50"
                            >
                              {uploadingClipId === clip.id
                                ? "Re-checking Rights & Queueing..."
                                : draft.scheduledAt
                                  ? "Schedule Short Upload"
                                  : "Publish Short Now"}
                            </button>
                          </form>
                        )}
                      </div>
                    ) : (
                      <p className="text-center text-[11px] text-slate-500">
                        Approve clip to enable YouTube upload.
                      </p>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </>
    )}
    </section>
  );
}
