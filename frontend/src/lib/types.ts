export type JobStatus =
  | "pending_rights"
  | "rejected"
  | "queued"
  | "processing"
  | "ready"
  | "failed";

export type RightsBasis = "owner" | "campaign" | "cc" | "permission";

export type ReviewStatus = "pending" | "approved" | "rejected";

export type SourceMode = "url" | "uploaded_file";

export interface YouTubeChannel {
  id: string;
  channel_id: string;
  title: string;
  handle: string | null;
  thumbnail_url: string | null;
  scopes: string;
  connected_at: string;
}

export interface UserProfile {
  id: string;
  email: string;
  name: string | null;
  avatar_url: string | null;
  created_at: string;
  youtube_channels: YouTubeChannel[];
}

export interface CampaignSource {
  id: string;
  campaign_id: string;
  source_url: string;
  normalized_video_id: string | null;
  source_channel_id: string | null;
  source_type: string;
  title: string | null;
  created_at: string;
}

export interface Campaign {
  id: string;
  owner_user_id: string;
  name: string;
  description: string | null;
  rules_text: string;
  deadline: string | null;
  is_active: boolean;
  created_at: string;
  updated_at: string;
  sources: CampaignSource[];
}

export interface PermissionDoc {
  id: string;
  user_id: string;
  channel_id: string;
  channel_name: string | null;
  original_filename: string;
  document_sha256: string;
  notes: string | null;
  expires_at: string | null;
  revoked: boolean;
  created_at: string;
}

export interface ClipItem {
  id: string;
  job_id: string;
  file_path: string;
  preview_url: string | null;
  start_sec: number;
  end_sec: number;
  score: number | null;
  title: string | null;
  attribution_text: string | null;
  review_status: ReviewStatus;
  created_at: string;
}

export interface JobItem {
  id: string;
  user_id: string;
  source_url: string;
  youtube_video_id: string;
  source_channel_id: string | null;
  source_title: string | null;
  source_duration_sec: number | null;
  campaign_id: string | null;
  rights_basis: RightsBasis | null;
  rights_evidence_json: Record<string, unknown>;
  rejection_reason: string | null;
  settings_json: Record<string, unknown>;
  source_mode?: SourceMode;
  uploaded_filename?: string | null;
  status: JobStatus;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  clips: ClipItem[];
}

