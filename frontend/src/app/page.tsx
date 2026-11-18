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

  // Poll automatically while any job is queued or processing
  useEffect(() => {
    if (!user) return;
    const hasActiveJob = jobs.some(
      (j) => j.status === "queued" || j.status === "processing"
    );
    if (!hasActiveJob) return;

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
            onClipUpdated={handleClipUpdated}
          />
        )}
      </main>
    </div>
  );
}

