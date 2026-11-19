"use client";

import { shouldSuggestFileUpload } from "@/lib/api";
import type { JobItem } from "@/lib/types";
import { RightsBasisBadge, StatusBadge, VerificationLevelBadge } from "./Badges";

interface JobsListProps {
  jobs: JobItem[];
  selectedJobId: string | null;
  onSelectJob: (jobId: string) => void;
  onRefresh: () => void;
  refreshing: boolean;
}

export function JobsList({
  jobs,
  selectedJobId,
  onSelectJob,
  onRefresh,
  refreshing,
}: JobsListProps) {
  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/70 p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-white">Clipping Jobs</h2>
          <p className="text-xs text-slate-400">
            Select a job to inspect its rights evidence and preview vertical clips.
          </p>
        </div>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-medium text-slate-300 transition hover:border-slate-500 hover:text-white disabled:opacity-50"
        >
          {refreshing ? "Refreshing..." : "Refresh"}
        </button>
      </div>

      {jobs.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-800 bg-slate-950/50 p-8 text-center text-sm text-slate-400">
          No jobs yet. Paste a YouTube URL above to test the Rights Gate and generate clips.
        </div>
      ) : (
        <div className="space-y-3">
          {jobs.map((job) => {
            const isSelected = job.id === selectedJobId;
            return (
              <button
                key={job.id}
                type="button"
                onClick={() => onSelectJob(job.id)}
                className={`w-full rounded-xl border p-4 text-left transition ${
                  isSelected
                    ? "border-indigo-500/80 bg-indigo-950/25"
                    : "border-slate-800 bg-slate-950/60 hover:border-slate-700"
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium text-white">
                      {job.source_title || job.source_url}
                    </p>
                    <p className="mt-0.5 text-xs text-slate-400">
                      Video ID: {job.youtube_video_id}
                      {job.clips.length > 0 && (
                        <> · {job.clips.length} clip{job.clips.length === 1 ? "" : "s"}</>
                      )}
                      {" · "}
                      {new Date(job.created_at).toLocaleString()}
                    </p>
                  </div>

                  <div className="flex flex-wrap items-center gap-1.5">
                    {job.source_mode === "uploaded_file" && (
                      <span className="inline-flex items-center rounded-full border border-slate-700 bg-slate-800/80 px-2 py-0.5 text-[11px] font-medium text-slate-200">
                        Uploaded File
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
                  <p className="mt-2.5 rounded-lg border border-rose-900/70 bg-rose-950/40 px-3 py-2 text-xs text-rose-200">
                    {job.rejection_reason}
                  </p>
                )}

                {job.status === "failed" && job.error_message && (
                  <div className="mt-2.5 rounded-lg border border-red-900/70 bg-red-950/40 px-3 py-2 text-xs text-red-200">
                    <p className="line-clamp-2">{job.error_message}</p>
                    {shouldSuggestFileUpload(job.error_message) && (
                      <p className="mt-1 text-[11px] text-amber-200">
                        Tip: Resubmit this YouTube URL using &ldquo;Upload source file instead of downloading&rdquo; above.
                      </p>
                    )}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      )}
    </section>
  );
}

