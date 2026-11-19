"use client";

import { useState } from "react";
import { createCampaign, uploadPermissionDocument } from "@/lib/api";
import type { Campaign, PermissionDoc } from "@/lib/types";

interface RightsSetupPanelProps {
  campaigns: Campaign[];
  permissions: PermissionDoc[];
  onCampaignCreated: (campaign: Campaign) => void;
  onPermissionUploaded: (permission: PermissionDoc) => void;
}

export function RightsSetupPanel({
  campaigns,
  permissions,
  onCampaignCreated,
  onPermissionUploaded,
}: RightsSetupPanelProps) {
  const [openTab, setOpenTab] = useState<"none" | "campaign" | "permission">("none");

  // Campaign form state
  const [campName, setCampName] = useState("");
  const [campRules, setCampRules] = useState("");
  const [campDeadline, setCampDeadline] = useState("");
  const [campSourcesRaw, setCampSourcesRaw] = useState("");
  const [campProofUrl, setCampProofUrl] = useState("");
  const [campProofFile, setCampProofFile] = useState<File | null>(null);
  const [campAttested, setCampAttested] = useState(false);
  const [campSaving, setCampSaving] = useState(false);
  const [campMsg, setCampMsg] = useState<string | null>(null);

  // Permission form state
  const [permChannelId, setPermChannelId] = useState("");
  const [permChannelName, setPermChannelName] = useState("");
  const [permScope, setPermScope] = useState("");
  const [permFile, setPermFile] = useState<File | null>(null);
  const [permSaving, setPermSaving] = useState(false);
  const [permMsg, setPermMsg] = useState<string | null>(null);

  async function handleCreateCampaign(e: React.FormEvent) {
    e.preventDefault();
    if (!campAttested) {
      setCampMsg("You must check the attestation checkbox to create a campaign.");
      return;
    }
    if (!campProofUrl.trim() && !campProofFile) {
      setCampMsg("You must provide either a Terms/Campaign Page URL or upload a proof document.");
      return;
    }

    setCampSaving(true);
    setCampMsg(null);
    try {
      const urls = campSourcesRaw
        .split(/\r?\n/)
        .map((line) => line.trim())
        .filter(Boolean);

      let created: Campaign;
      if (campProofFile) {
        const formData = new FormData();
        formData.append("name", campName.trim());
        if (campRules.trim()) formData.append("rules_text", campRules.trim());
        if (campDeadline) {
          formData.append("deadline", new Date(campDeadline).toISOString());
        }
        if (campProofUrl.trim()) formData.append("proof_url", campProofUrl.trim());
        formData.append("attested", "true");
        formData.append("sources", JSON.stringify(urls.map((source_url) => ({ source_url }))));
        formData.append("proof_file", campProofFile);
        created = await createCampaign(formData);
      } else {
        created = await createCampaign({
          name: campName.trim(),
          rules_text: campRules.trim(),
          deadline: campDeadline ? new Date(campDeadline).toISOString() : null,
          proof_url: campProofUrl.trim(),
          attested: true,
          sources: urls.map((source_url) => ({ source_url })),
        });
      }

      onCampaignCreated(created);
      setCampName("");
      setCampRules("");
      setCampDeadline("");
      setCampSourcesRaw("");
      setCampProofUrl("");
      setCampProofFile(null);
      setCampAttested(false);
      setCampMsg(`Created campaign "${created.name}".`);
    } catch (err) {
      setCampMsg(
        err instanceof Error ? err.message : "Failed to create campaign."
      );
    } finally {
      setCampSaving(false);
    }
  }

  async function handleUploadPermission(e: React.FormEvent) {
    e.preventDefault();
    if (!permFile) return;
    setPermSaving(true);
    setPermMsg(null);
    try {
      const formData = new FormData();
      formData.append("channel_id", permChannelId.trim());
      if (permChannelName.trim()) {
        formData.append("channel_name", permChannelName.trim());
      }
      if (permScope.trim()) {
        formData.append("scope", permScope.trim());
      }
      formData.append("file", permFile);
      const uploaded = await uploadPermissionDocument(formData);
      onPermissionUploaded(uploaded);
      setPermChannelId("");
      setPermChannelName("");
      setPermScope("");
      setPermFile(null);
      setPermMsg(`Uploaded written permission for channel ${uploaded.channel_id}.`);
    } catch (err) {
      setPermMsg(
        err instanceof Error ? err.message : "Failed to upload permission."
      );
    } finally {
      setPermSaving(false);
    }
  }

  return (
    <section className="rounded-2xl border border-slate-800 bg-slate-900/50 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs text-slate-400">
          Rights Sources: <span className="text-slate-200">{campaigns.length} campaign(s)</span>
          {" · "}
          <span className="text-slate-200">{permissions.length} written permission(s)</span>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() =>
              setOpenTab((t) => (t === "campaign" ? "none" : "campaign"))
            }
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-medium text-slate-200 hover:border-violet-500"
          >
            {openTab === "campaign" ? "Close Campaign Form" : "+ New Campaign"}
          </button>
          <button
            type="button"
            onClick={() =>
              setOpenTab((t) => (t === "permission" ? "none" : "permission"))
            }
            className="rounded-lg border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs font-medium text-slate-200 hover:border-cyan-500"
          >
            {openTab === "permission"
              ? "Close Permission Form"
              : "+ Upload Permission"}
          </button>
        </div>
      </div>

      {openTab === "campaign" && (
        <form
          onSubmit={handleCreateCampaign}
          className="mt-4 space-y-3 border-t border-slate-800 pt-4"
        >
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-slate-300">
                Campaign Name
              </label>
              <input
                type="text"
                required
                value={campName}
                onChange={(e) => setCampName(e.target.value)}
                placeholder="Podcast Launch Clipping"
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-violet-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-300">
                Deadline (Optional)
              </label>
              <input
                type="datetime-local"
                value={campDeadline}
                onChange={(e) => setCampDeadline(e.target.value)}
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-violet-500 focus:outline-none"
              />
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-slate-300">
                Proof: Terms / Campaign Page URL
              </label>
              <input
                type="url"
                value={campProofUrl}
                onChange={(e) => setCampProofUrl(e.target.value)}
                placeholder="https://creator.com/clipping-terms"
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-violet-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-300">
                Or Upload Proof File (.pdf, .png, .jpg, .docx, .html)
              </label>
              <input
                type="file"
                accept=".pdf,.png,.jpg,.jpeg,.txt,.docx,.html"
                onChange={(e) => setCampProofFile(e.target.files?.[0] || null)}
                className="block w-full text-xs text-slate-300 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white hover:file:bg-slate-700"
              />
            </div>
          </div>

          <div>
            <label className="mb-1 block text-xs text-slate-300">
              Authorized YouTube URLs (one per line)
            </label>
            <textarea
              rows={3}
              required
              value={campSourcesRaw}
              onChange={(e) => setCampSourcesRaw(e.target.value)}
              placeholder={"https://www.youtube.com/watch?v=...\nhttps://youtu.be/..."}
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-violet-500 focus:outline-none"
            />
          </div>

          <div>
            <label className="mb-1 block text-xs text-slate-300">
              Campaign Rules
            </label>
            <textarea
              rows={2}
              value={campRules}
              onChange={(e) => setCampRules(e.target.value)}
              placeholder="Include #CampaignTag in descriptions. No misleading edits."
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-violet-500 focus:outline-none"
            />
          </div>

          <div className="rounded-lg border border-violet-800/60 bg-violet-950/30 p-2.5">
            <label className="flex items-start gap-2 text-xs text-violet-200 cursor-pointer">
              <input
                type="checkbox"
                required
                checked={campAttested}
                onChange={(e) => setCampAttested(e.target.checked)}
                className="mt-0.5 h-3.5 w-3.5 rounded border-slate-700 bg-slate-900 text-violet-600 focus:ring-violet-500"
              />
              <span>
                <strong>Rights Attestation (Required):</strong> I attest that I have verified rights/authorization to clip these videos under these campaign terms.
              </span>
            </label>
          </div>

          <div className="flex items-center justify-between gap-3">
            {campMsg ? (
              <span className="text-xs text-violet-200">{campMsg}</span>
            ) : (
              <span />
            )}
            <button
              type="submit"
              disabled={campSaving || !campAttested}
              className="rounded-lg bg-violet-600 px-4 py-2 text-xs font-semibold text-white hover:bg-violet-500 disabled:opacity-50"
            >
              {campSaving ? "Creating..." : "Create Campaign"}
            </button>
          </div>
        </form>
      )}

      {openTab === "permission" && (
        <form
          onSubmit={handleUploadPermission}
          className="mt-4 space-y-3 border-t border-slate-800 pt-4"
        >
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-slate-300">
                YouTube Channel ID (UC...)
              </label>
              <input
                type="text"
                required
                value={permChannelId}
                onChange={(e) => setPermChannelId(e.target.value)}
                placeholder="UCxxxxxxxxxxxxxxxxxxxxxx"
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-cyan-500 focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-slate-300">
                Channel Name (Optional)
              </label>
              <input
                type="text"
                value={permChannelName}
                onChange={(e) => setPermChannelName(e.target.value)}
                placeholder="Partner Creator"
                className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-cyan-500 focus:outline-none"
              />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-300">
              Scope of Permission (Optional free-text)
            </label>
            <input
              type="text"
              value={permScope}
              onChange={(e) => setPermScope(e.target.value)}
              placeholder="e.g. YouTube Shorts only, clips up to 60s, non-commercial"
              className="w-full rounded-lg border border-slate-700 bg-slate-950 px-3 py-2 text-xs text-white focus:border-cyan-500 focus:outline-none"
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-slate-300">
              Written Permission Document (.pdf, .png, .jpg, .txt, .docx)
            </label>
            <input
              type="file"
              required
              accept=".pdf,.png,.jpg,.jpeg,.txt,.docx"
              onChange={(e) => setPermFile(e.target.files?.[0] || null)}
              className="block w-full text-xs text-slate-300 file:mr-3 file:rounded-lg file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-white hover:file:bg-slate-700"
            />
          </div>
          <div className="flex items-center justify-between gap-3">
            {permMsg ? (
              <span className="text-xs text-cyan-200">{permMsg}</span>
            ) : (
              <span />
            )}
            <button
              type="submit"
              disabled={permSaving || !permFile}
              className="rounded-lg bg-cyan-600 px-4 py-2 text-xs font-semibold text-white hover:bg-cyan-500 disabled:opacity-50"
            >
              {permSaving ? "Uploading..." : "Upload Written Permission"}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
