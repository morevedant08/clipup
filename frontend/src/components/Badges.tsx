"use client";

import type { JobStatus, ReviewStatus, RightsBasis } from "@/lib/types";

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

