"use client";

import { useState } from "react";
import { getYouTubeConnectUrl } from "@/lib/api";
import type { UserProfile } from "@/lib/types";

interface NavbarProps {
  user: UserProfile;
  onLogout: () => void;
}

export function Navbar({ user, onLogout }: NavbarProps) {
  const [connecting, setConnecting] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  async function handleConnectYouTube() {
    setConnecting(true);
    setConnectError(null);
    try {
      const url = await getYouTubeConnectUrl();
      window.location.href = url;
    } catch (err) {
      setConnectError(
        err instanceof Error ? err.message : "Failed to initiate YouTube connect."
      );
      setConnecting(false);
    }
  }

  return (
    <header className="border-b border-slate-800/80 bg-slate-950/80 backdrop-blur">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-4 px-4 py-3.5 sm:px-6">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-600 font-bold text-white shadow-sm">
            C
          </div>
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-base font-semibold tracking-tight text-white">
                Clipper
              </h1>
              <span className="rounded bg-emerald-950 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-emerald-300 ring-1 ring-emerald-800/70">
                Rights-Gated
              </span>
            </div>
            <p className="text-xs text-slate-400">
              {user.name || user.email}
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2.5">
          {user.youtube_channels.length > 0 ? (
            <div className="flex flex-wrap items-center gap-1.5">
              {user.youtube_channels.map((ch) => (
                <span
                  key={ch.id}
                  title={`Channel ID: ${ch.channel_id} (read-only)`}
                  className="inline-flex items-center gap-1.5 rounded-full border border-indigo-800/70 bg-indigo-950/60 px-2.5 py-1 text-xs text-indigo-200"
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" />
                  {ch.title}
                </span>
              ))}
            </div>
          ) : (
            <span className="text-xs text-slate-500">No channel connected</span>
          )}

          <button
            type="button"
            onClick={handleConnectYouTube}
            disabled={connecting}
            className="rounded-lg border border-slate-700 bg-slate-900 px-3 py-1.5 text-xs font-medium text-slate-200 transition hover:border-indigo-500 hover:text-white disabled:opacity-50"
          >
            {connecting ? "Redirecting..." : "Connect YouTube (Read-Only)"}
          </button>

          <button
            type="button"
            onClick={onLogout}
            className="rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-400 transition hover:bg-slate-800 hover:text-white"
          >
            Sign Out
          </button>
        </div>
      </div>
      {connectError && (
        <div className="mx-auto max-w-6xl px-4 pb-3 sm:px-6">
          <div className="rounded-md border border-rose-800/70 bg-rose-950/60 px-3 py-2 text-xs text-rose-200">
            {connectError}
          </div>
        </div>
      )}
    </header>
  );
}

