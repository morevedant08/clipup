"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { setStoredToken } from "@/lib/api";

export default function AuthCallbackPage() {
  const router = useRouter();

  useEffect(() => {
    const hash = window.location.hash.replace(/^#/, "");
    const params = new URLSearchParams(hash);
    const token = params.get("token");
    if (token) {
      setStoredToken(token);
      window.history.replaceState(null, "", "/auth/callback");
    }
    router.replace("/");
  }, [router]);

  return (
    <main className="flex min-h-screen items-center justify-center text-sm text-slate-400">
      Completing sign-in...
    </main>
  );
}

