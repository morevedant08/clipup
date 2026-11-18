"use client";

import { useState } from "react";
import { devLogin, getGoogleLoginUrl, setStoredToken } from "@/lib/api";
import type { UserProfile } from "@/lib/types";

interface LoginCardProps {
  onAuthenticated: (user: UserProfile) => void;
}

export function LoginCard({ onAuthenticated }: LoginCardProps) {
  const [loadingGoogle, setLoadingGoogle] = useState(false);
  const [showDevLogin, setShowDevLogin] = useState(false);
  const [devEmail, setDevEmail] = useState("creator@example.com");
  const [devName, setDevName] = useState("Local Creator");
  const [error, setError] = useState<string | null>(null);

  async function handleGoogleSignIn() {
    setLoadingGoogle(true);
    setError(null);
    try {
      const authUrl = await getGoogleLoginUrl();
      window.location.href = authUrl;
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to start Google sign-in. Check backend configuration."
      );
      setLoadingGoogle(false);
    }
  }

  async function handleDevSignIn(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      const result = await devLogin(devEmail, devName);
      setStoredToken(result.access_token);
      onAuthenticated(result.user);
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Dev login failed. Ensure ENABLE_DEV_AUTH=true in backend .env."
      );
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-md rounded-2xl border border-slate-800 bg-slate-900/80 p-8 shadow-2xl">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-600 text-lg font-bold text-white">
            C
          </div>
          <div>
            <h1 className="text-xl font-semibold text-white">Clipper</h1>
            <p className="text-xs text-slate-400">
              Rights-Safe Automated Video Clipping
            </p>
          </div>
        </div>

        <p className="mb-6 text-sm leading-relaxed text-slate-300">
          Every clipping job passes a mandatory{" "}
          <span className="font-medium text-emerald-300">Rights Gate</span>{" "}
          before any video is downloaded: channel ownership, authorized campaign
          source, Creative Commons license (with attribution), or uploaded
          written permission.
        </p>

        {error && (
          <div className="mb-4 rounded-lg border border-rose-800/80 bg-rose-950/60 p-3 text-xs text-rose-200">
            {error}
          </div>
        )}

        <button
          type="button"
          onClick={handleGoogleSignIn}
          disabled={loadingGoogle}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-indigo-600 px-4 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-500 disabled:opacity-50"
        >
          {loadingGoogle ? "Connecting to Google..." : "Continue with Google"}
        </button>

        <p className="mt-3 text-center text-[11px] text-slate-500">
          Uses read-only profile &amp; YouTube metadata scopes only. No upload
          permissions are requested in this phase.
        </p>

        <div className="mt-6 border-t border-slate-800 pt-4">
          <button
            type="button"
            onClick={() => setShowDevLogin((v) => !v)}
            className="text-xs text-slate-500 underline-offset-2 hover:text-slate-300 hover:underline"
          >
            {showDevLogin ? "Hide local dev sign-in" : "Local dev sign-in (if enabled)"}
          </button>

          {showDevLogin && (
            <form onSubmit={handleDevSignIn} className="mt-3 space-y-3">
              <div>
                <label className="mb-1 block text-xs text-slate-400">
                  Email
                </label>
                <input
                  type="email"
                  required
                  value={devEmail}
                  onChange={(e) => setDevEmail(e.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white focus:border-indigo-500 focus:outline-none"
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-slate-400">
                  Display Name
                </label>
                <input
                  type="text"
                  value={devName}
                  onChange={(e) => setDevName(e.target.value)}
                  className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white focus:border-indigo-500 focus:outline-none"
                />
              </div>
              <button
                type="submit"
                className="w-full rounded-lg border border-slate-700 bg-slate-800 px-3 py-2 text-xs font-medium text-slate-200 transition hover:bg-slate-700"
              >
                Sign In Locally
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}

