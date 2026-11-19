"use client";

import type {
  JobStatus,
  ReviewStatus,
  RightsBasis,
  UploadStatus,
} from "@/lib/types";

const STATUS_STYLES: Record<JobStatus, string> = {
  pending_rights: "bg-slate-800 text-slate-300 border-slate-700",
  rejected: "bg-rose-950/70 text-rose-300 border-rose-800/80",
  queued: "bg-amber-950/70 text-amber-300 border-amber-800/80",
  processing: "bg-sky-950/70 text-sky-300 border-sky-800/80 animate-pulse",
  ready: "bg-emerald-950/70 text-emerald-300 border-emerald-800/80",
  failed: "bg-red-950/80 text-red-300 border-red-800/80",
};

const STATUS_LABELS: Record<JobStatus, string> = {
  pending_rights: "Pending Rights",
  rejected: "Rejected",
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};

const RIGHTS_STYLES: Record<RightsBasis, string> = {
  owner: "bg-indigo-950/80 text-indigo-300 border-indigo-700/70",
  campaign: "bg-violet-950/80 text-violet-300 border-violet-700/70",
  cc: "bg-teal-950/80 text-teal-300 border-teal-700/70",
  permission: "bg-cyan-950/80 text-cyan-300 border-cyan-700/70",
};

const RIGHTS_LABELS: Record<RightsBasis, string> = {
  owner: "Owner Channel",
  campaign: "Campaign Source",
  cc: "Creative Commons (Attribution)",
  permission: "Written Permission",
};

export function StatusBadge({ status }: { status: JobStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${STATUS_STYLES[status]}`}
    >
      {STATUS_LABELS[status]}
    </span>
  );
}

export function RightsBasisBadge({ basis }: { basis: RightsBasis | null }) {
  if (!basis) {
    return (
      <span className="inline-flex items-center rounded-full border border-rose-800/70 bg-rose-950/50 px-2.5 py-0.5 text-xs font-medium text-rose-300">
        No Rights Match
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-medium ${RIGHTS_STYLES[basis]}`}
    >
      Rights: {RIGHTS_LABELS[basis]}
    </span>
  );
}

export function ReviewBadge({ status }: { status: ReviewStatus }) {
  const styles: Record<ReviewStatus, string> = {
    pending: "border-slate-700 bg-slate-800/80 text-slate-300",
    approved: "border-emerald-700 bg-emerald-950/80 text-emerald-300",
    rejected: "border-rose-700 bg-rose-950/80 text-rose-300",
  };
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${styles[status]}`}
    >
      {status}
    </span>
  );
}

export function UploadStatusBadge({
  status,
  scheduledAt,
}: {
  status: UploadStatus;
  scheduledAt?: string | null;
}) {
  const styles: Record<UploadStatus, string> = {
    queued: "border-amber-700/80 bg-amber-950/70 text-amber-200",
    uploading: "border-sky-700/80 bg-sky-950/70 text-sky-200 animate-pulse",
    uploaded: "border-emerald-700/80 bg-emerald-950/80 text-emerald-200",
    failed: "border-rose-700/80 bg-rose-950/70 text-rose-200",
  };
  const label =
    status === "queued" && scheduledAt
      ? "Scheduled"
      : status.charAt(0).toUpperCase() + status.slice(1);
  return (
    <span
      className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${styles[status]}`}
    >
      Upload: {label}
    </span>
  );
}

export function VerificationLevelBadge({
  level,
}: {
  level?: "api_verified" | "self_attested" | string | null;
}) {
  if (!level) return null;
  if (level === "api_verified") {
    return (
      <span
        title="Directly verified via Google / YouTube API"
        className="inline-flex items-center rounded-full border border-emerald-600/80 bg-emerald-950/70 px-2 py-0.5 text-[11px] font-medium text-emerald-300"
      >
        <span className="mr-1 h-1.5 w-1.5 rounded-full bg-emerald-400" />
        API Verified
      </span>
    );
  }
  if (level === "self_attested") {
    return (
      <span
        title="Self-attested rights (campaign rules or written permission document)"
        className="inline-flex items-center rounded-full border border-amber-600/80 bg-amber-950/70 px-2 py-0.5 text-[11px] font-medium text-amber-300"
      >
        <span className="mr-1 h-1.5 w-1.5 rounded-full bg-amber-400" />
        Self-Attested
      </span>
    );
  }
  return null;
}


