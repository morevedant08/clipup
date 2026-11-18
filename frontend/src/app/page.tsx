"use client";

import { useCallback, useEffect, useState } from "react";
import { ClipPreviewGrid } from "@/components/ClipPreviewGrid";
import { JobsList } from "@/components/JobsList";
import { LoginCard } from "@/components/LoginCard";
import { Navbar } from "@/components/Navbar";
import { NewJobForm } from "@/components/NewJobForm";
import { RightsSetupPanel } from "@/components/RightsSetupPanel";
import {
  clearStoredToken,
  fetchCampaigns,
  fetchCurrentUser,
  fetchJobs,
  fetchPermissions,
  getStoredToken,
} from "@/lib/api";
import type {
  Campaign,
  ClipItem,
  JobItem,
  PermissionDoc,
  UploadItem,
  UserProfile,
} from "@/lib/types";

export default function HomePage() {
  const [booting, setBooting] = useState(true);
  const [user, setUser] = useState<UserProfile | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [permissions, setPermissions] = useState<PermissionDoc[]>([]);
  const [jobs, setJobs] = useState<JobItem[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [oauthNotice, setOauthNotice] = useState<string | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("youtube_connected") === "1") {
      const count = params.get("channels") || "1";
      setOauthNotice(
        `Connected ${count} YouTube channel(s) with read-only permissions.`
      );
      window.history.replaceState(null, "", "/");
    } else if (params.get("youtube_upload_enabled") === "1") {
      const count = params.get("channels") || "1";
      setOauthNotice(
        `Enabled YouTube Shorts uploading for ${count} channel(s).`
      );
      window.history.replaceState(null, "", "/");
    }
  }, []);

  const loadWorkspaceData = useCallback(async () => {
    setRefreshing(true);
    try {
      const [campList, permList, jobList] = await Promise.all([
        fetchCampaigns(),
        fetchPermissions(),
        fetchJobs(),
      ]);
      setCampaigns(campList);
      setPermissions(permList);
      setJobs(jobList);
      setSelectedJobId((prev) =>
        prev && jobList.some((j) => j.id === prev)
          ? prev
          : jobList[0]?.id ?? null
      );
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    const token = getStoredToken();
    if (!token) {
      setBooting(false);
      return;
    }
    fetchCurrentUser()
      .then(async (profile) => {
        setUser(profile);
        await loadWorkspaceData();
      })
      .catch(() => {
        clearStoredToken();
        setUser(null);
      })
      .finally(() => setBooting(false));
  }, [loadWorkspaceData]);

  // Poll automatically while any job or immediate upload is in progress
  useEffect(() => {
    if (!user) return;
    const hasActiveWork = jobs.some(
      (j) =>
        j.status === "queued" ||
        j.status === "processing" ||
        j.clips.some(
          (c) =>
            c.latest_upload?.status === "uploading" ||
            (c.latest_upload?.status === "queued" &&
              !c.latest_upload.scheduled_at)
        )
    );
    if (!hasActiveWork) return;

    const timer = window.setInterval(() => {
      fetchJobs()
        .then((updated) => setJobs(updated))
        .catch(() => {
          /* ignore transient poll error */
        });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [user, jobs]);

  function handleLogout() {
    clearStoredToken();
    setUser(null);
    setJobs([]);
    setCampaigns([]);
    setPermissions([]);
    setSelectedJobId(null);
  }

  function handleAuthenticated(profile: UserProfile) {
    setUser(profile);
    loadWorkspaceData().catch(() => {
      /* handled in UI */
    });
  }

  function handleJobCreated(job: JobItem) {
    setJobs((prev) => [job, ...prev]);
    setSelectedJobId(job.id);
  }

  function handleClipUpdated(jobId: string, updatedClip: ClipItem) {
    setJobs((prev) =>
      prev.map((job) =>
        job.id !== jobId
          ? job
          : {
              ...job,
              clips: job.clips.map((c) =>
                c.id === updatedClip.id ? updatedClip : c
              ),
            }
      )
    );
  }

  function handleUploadQueued(
    jobId: string,
    clipId: string,
    upload: UploadItem
  ) {
    setJobs((prev) =>
      prev.map((job) =>
        job.id !== jobId
          ? job
          : {
              ...job,
              clips: job.clips.map((c) =>
                c.id === clipId ? { ...c, latest_upload: upload } : c
              ),
            }
      )
    );
  }

  if (booting) {
    return (
      <main className="flex min-h-screen items-center justify-center text-sm text-slate-400">
        Loading Clipper...
      </main>
    );
  }

  if (!user) {
    return <LoginCard onAuthenticated={handleAuthenticated} />;
  }

  const selectedJob = jobs.find((j) => j.id === selectedJobId) || null;

  return (
    <div className="min-h-screen">
      <Navbar user={user} onLogout={handleLogout} />
      <main className="mx-auto max-w-6xl space-y-6 px-4 py-8 sm:px-6">
        {oauthNotice && (
          <div className="flex items-center justify-between rounded-xl border border-emerald-800/70 bg-emerald-950/60 px-4 py-3 text-xs text-emerald-200">
            <span>{oauthNotice}</span>
            <button
              type="button"
              onClick={() => setOauthNotice(null)}
              className="text-emerald-300 hover:text-white"
            >
              Dismiss
            </button>
          </div>
        )}
        <NewJobForm campaigns={campaigns} onJobCreated={handleJobCreated} />

        <RightsSetupPanel
          campaigns={campaigns}
          permissions={permissions}
          onCampaignCreated={(camp) => setCampaigns((prev) => [camp, ...prev])}
          onPermissionUploaded={(perm) =>
            setPermissions((prev) => [perm, ...prev])
          }
        />

        <JobsList
          jobs={jobs}
          selectedJobId={selectedJobId}
          onSelectJob={setSelectedJobId}
          onRefresh={() => loadWorkspaceData()}
          refreshing={refreshing}
        />

        {selectedJob && (
          <ClipPreviewGrid
            job={selectedJob}
            uploadEnabled={user.youtube_channels.some((c) => c.upload_enabled)}
            onClipUpdated={handleClipUpdated}
            onUploadQueued={handleUploadQueued}
          />
        )}
      </main>
    </div>
  );
}

