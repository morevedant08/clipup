"use client";

import { useRef, useState } from "react";
import { createJob } from "@/lib/api";
import type { Campaign, JobItem } from "@/lib/types";
import { RightsBasisBadge, StatusBadge } from "./Badges";

interface NewJobFormProps {
  campaigns: Campaign[];
  onJobCreated: (job: JobItem) => void;
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function NewJobForm({ campaigns, onJobCreated }: NewJobFormProps) {
  const [sourceUrl, setSourceUrl] = useState("");
  const [campaignId, setCampaignId] = useState<string>("");
  const [clipCount, setClipCount] = useState<number>(3);
  const [clipDuration, setClipDuration] = useState<number>(30);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastOutcome, setLastOutcome] = useState<JobItem | null>(null);

  const selectedCampaign = campaigns.find((c) => c.id === campaignId) || null;

  function clearSelectedFile() {
    setSourceFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    setLastOutcome(null);

    try {
      const job = await createJob({
        source_url: sourceUrl.trim(),
        campaign_id: campaignId || null,
        settings: {
          clip_count: clipCount,
          clip_duration_sec: clipDuration,
          aspect_ratio: "9:16",
        },
        source_file: sourceFile,
      });
      setLastOutcome(job);
      onJobCreated(job);
      if (job.status !== "rejected") {
        setSourceUrl("");
        clearSelectedFile();
      }
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to submit clipping job."
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/70 p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-white">New Clipping Job</h2>
          <p className="text-xs text-slate-400">
            Paste a YouTube video URL. Clipper runs the Rights Gate on the URL first before downloading or accepting an uploaded file.
          </p>
        </div>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="grid gap-4 md:grid-cols-3">
          <div className="md:col-span-2">
            <label
              htmlFor="source-url"
              className="mb-1.5 block text-xs font-medium text-slate-300"
            >
              YouTube Video URL
            </label>
            <input
              id="source-url"
              type="url"
              required
              placeholder="https://www.youtube.com/watch?v=..."
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              className="w-full rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-2.5 text-sm text-white placeholder-slate-500 focus:border-indigo-500 focus:outline-none"
            />
          </div>

          <div>
            <label
              htmlFor="campaign-select"
              className="mb-1.5 block text-xs font-medium text-slate-300"
            >
              Paid Campaign (Optional)
            </label>
            <select
              id="campaign-select"
              value={campaignId}
              onChange={(e) => setCampaignId(e.target.value)}
              className="w-full rounded-xl border border-slate-700 bg-slate-950 px-3.5 py-2.5 text-sm text-white focus:border-indigo-500 focus:outline-none"
            >
              <option value="">None (Owner / CC / Permission)</option>
              {campaigns.map((camp) => (
                <option key={camp.id} value={camp.id}>
                  {camp.name} ({camp.sources.length} source
                  {camp.sources.length === 1 ? "" : "s"})
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-3.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label
              htmlFor="source-file"
              className="text-xs font-medium text-slate-300"
            >
              Upload source file instead of downloading{" "}
              <span className="font-normal text-slate-500">(optional, MP4 / MOV / WebM)</span>
            </label>
            {sourceFile && (
              <button
                type="button"
                onClick={clearSelectedFile}
                className="text-xs text-slate-400 hover:text-white"
              >
                Clear file
              </button>
            )}
          </div>
          <input
            ref={fileInputRef}
            id="source-file"
            type="file"
            accept=".mp4,.mov,.webm,video/mp4,video/quicktime,video/webm"
            onChange={(e) => setSourceFile(e.target.files?.[0] || null)}
            className="mt-2 block w-full text-xs text-slate-300 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white hover:file:bg-slate-700"
          />
          {sourceFile ? (
            <p className="mt-1.5 text-[11px] text-indigo-300">
              Selected: {sourceFile.name} ({formatFileSize(sourceFile.size)}) — will skip YouTube download after the Rights Gate approves the URL.
            </p>
          ) : (
            <p className="mt-1.5 text-[11px] text-slate-500">
              Leave blank to download automatically from YouTube. Use this if you already have the source video file or if YouTube blocks automated downloads.
            </p>
          )}
        </div>

        {selectedCampaign && (
          <div className="rounded-xl border border-violet-800/60 bg-violet-950/30 p-3 text-xs text-violet-200">
            <div className="flex flex-wrap items-center justify-between gap-2 font-medium">
              <span>Campaign: {selectedCampaign.name}</span>
              {selectedCampaign.deadline && (
                <span className="text-violet-300">
                  Deadline: {new Date(selectedCampaign.deadline).toLocaleString()}
                </span>
              )}
            </div>
            {selectedCampaign.rules_text && (
              <p className="mt-1 text-violet-200/80">
                Rules: {selectedCampaign.rules_text}
              </p>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-end justify-between gap-4">
          <div className="flex flex-wrap items-center gap-4">
            <div>
              <label
                htmlFor="clip-count"
                className="mb-1 block text-xs text-slate-400"
              >
                Clips to generate
              </label>
              <input
                id="clip-count"
                type="number"
                min={1}
                max={10}
                value={clipCount}
                onChange={(e) => setClipCount(Number(e.target.value) || 3)}
                className="w-24 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white focus:border-indigo-500 focus:outline-none"
              />
            </div>

            <div>
              <label
                htmlFor="clip-duration"
                className="mb-1 block text-xs text-slate-400"
              >
                Target length (sec)
              </label>
              <input
                id="clip-duration"
                type="number"
                min={5}
                max={120}
                value={clipDuration}
                onChange={(e) => setClipDuration(Number(e.target.value) || 30)}
                className="w-28 rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white focus:border-indigo-500 focus:outline-none"
              />
            </div>

            <div className="pb-2 text-xs text-slate-500">
              Format: <span className="font-medium text-slate-300">9:16 Vertical (1080×1920)</span>
            </div>
          </div>

          <button
            type="submit"
            disabled={submitting}
            className="rounded-xl bg-indigo-600 px-5 py-2.5 text-sm font-semibold text-white transition hover:bg-indigo-500 disabled:opacity-50"
          >
            {submitting
              ? sourceFile
                ? "Verifying Rights & Uploading..."
                : "Checking Rights..."
              : sourceFile
                ? "Verify Rights & Upload Source"
                : "Verify Rights & Queue Job"}
          </button>
        </div>
      </form>

      {error && (
        <div
          role="alert"
          className="mt-4 rounded-xl border border-rose-800/80 bg-rose-950/60 p-4 text-sm text-rose-200"
        >
          <p className="font-semibold text-rose-100">Submission Error</p>
          <p className="mt-1 text-xs leading-relaxed">{error}</p>
        </div>
      )}

      {lastOutcome && lastOutcome.status === "rejected" && (
        <div
          role="alert"
          className="mt-4 rounded-xl border border-rose-700/80 bg-rose-950/70 p-4"
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-rose-100">
              Rights Gate Rejected This Video
            </span>
            <StatusBadge status={lastOutcome.status} />
            <RightsBasisBadge basis={lastOutcome.rights_basis} />
          </div>
          <p className="mt-2 text-xs leading-relaxed text-rose-200">
            {lastOutcome.rejection_reason}
          </p>
        </div>
      )}

      {lastOutcome && lastOutcome.status === "queued" && (
        <div className="mt-4 rounded-xl border border-emerald-800/70 bg-emerald-950/50 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-emerald-100">
              {lastOutcome.source_mode === "uploaded_file"
                ? "Rights Verified — Uploaded Source Queued"
                : "Rights Verified — Job Queued"}
            </span>
            <StatusBadge status={lastOutcome.status} />
            <RightsBasisBadge basis={lastOutcome.rights_basis} />
          </div>
          {lastOutcome.rights_basis === "cc" &&
            typeof lastOutcome.rights_evidence_json?.attribution_text === "string" && (
              <p className="mt-2 text-xs text-teal-200">
                Required Attribution:{" "}
                {lastOutcome.rights_evidence_json.attribution_text}
              </p>
            )}
        </div>
      )}
    </section>
  );
}
