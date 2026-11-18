import type {
  Campaign,
  ClipItem,
  JobItem,
  PermissionDoc,
  ReviewStatus,
  UploadItem,
  UploadPrivacy,
  UserProfile,
} from "./types";

const TOKEN_STORAGE_KEY = "clipper_access_token";

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
).replace(/\/$/, "");

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setStoredToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearStoredToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function apiFetch<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const token = getStoredToken();
  const headers = new Headers(options.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (
    options.body &&
    !(options.body instanceof FormData) &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  const res = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    credentials: "include",
    cache: "no-store",
  });

  if (res.status === 204) {
    return undefined as T;
  }

  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }

  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : `Request failed with status ${res.status}`;
    throw new ApiError(detail, res.status);
  }

  return data as T;
}

export function buildMediaUrl(previewPath: string | null): string | null {
  if (!previewPath) return null;
  const token = getStoredToken();
  const url = `${API_BASE_URL}${previewPath}`;
  return token ? `${url}?token=${encodeURIComponent(token)}` : url;
}

// --- Auth ---

export async function getGoogleLoginUrl(): Promise<string> {
  const data = await apiFetch<{ auth_url: string }>("/api/auth/google/login");
  return data.auth_url;
}

export async function devLogin(
  email: string,
  name: string
): Promise<{ access_token: string; user: UserProfile }> {
  return apiFetch<{ access_token: string; user: UserProfile }>(
    "/api/auth/dev-login",
    {
      method: "POST",
      body: JSON.stringify({ email, name }),
    }
  );
}

export async function fetchCurrentUser(): Promise<UserProfile> {
  return apiFetch<UserProfile>("/api/auth/me");
}

export async function getYouTubeConnectUrl(): Promise<string> {
  const data = await apiFetch<{ auth_url: string }>(
    "/api/auth/youtube/connect"
  );
  return data.auth_url;
}

export async function getYouTubeEnableUploadUrl(): Promise<string> {
  const data = await apiFetch<{ auth_url: string }>(
    "/api/auth/youtube/enable-upload"
  );
  return data.auth_url;
}

// --- Campaigns ---

export async function fetchCampaigns(): Promise<Campaign[]> {
  return apiFetch<Campaign[]>("/api/campaigns");
}

export async function createCampaign(payload: {
  name: string;
  description?: string;
  rules_text: string;
  deadline?: string | null;
  sources: { source_url: string; title?: string }[];
}): Promise<Campaign> {
  return apiFetch<Campaign>("/api/campaigns", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

// --- Permissions ---

export async function fetchPermissions(): Promise<PermissionDoc[]> {
  return apiFetch<PermissionDoc[]>("/api/permissions");
}

export async function uploadPermissionDocument(
  formData: FormData
): Promise<PermissionDoc> {
  return apiFetch<PermissionDoc>("/api/permissions", {
    method: "POST",
    body: formData,
  });
}

export function shouldSuggestFileUpload(
  errorMessage: string | null | undefined
): boolean {
  if (!errorMessage) return false;
  const lower = errorMessage.toLowerCase();
  return (
    lower.includes("sign-in") ||
    lower.includes("sign in") ||
    lower.includes("bot check") ||
    lower.includes("not a bot") ||
    lower.includes("rate-limit") ||
    lower.includes("rate limit") ||
    lower.includes("too many requests") ||
    lower.includes("age verification")
  );
}

// --- Jobs & Clips ---

export async function createJob(payload: {
  source_url: string;
  campaign_id?: string | null;
  settings: {
    clip_count: number;
    clip_duration_sec: number;
    aspect_ratio: "9:16";
  };
  source_file?: File | null;
}): Promise<JobItem> {
  if (payload.source_file) {
    const formData = new FormData();
    formData.append("source_url", payload.source_url);
    if (payload.campaign_id) {
      formData.append("campaign_id", payload.campaign_id);
    }
    formData.append("clip_count", String(payload.settings.clip_count));
    formData.append(
      "clip_duration_sec",
      String(payload.settings.clip_duration_sec)
    );
    formData.append("aspect_ratio", payload.settings.aspect_ratio);
    formData.append("source_file", payload.source_file);
    return apiFetch<JobItem>("/api/jobs", {
      method: "POST",
      body: formData,
    });
  }

  return apiFetch<JobItem>("/api/jobs", {
    method: "POST",
    body: JSON.stringify({
      source_url: payload.source_url,
      campaign_id: payload.campaign_id ?? null,
      settings: payload.settings,
    }),
  });
}

export async function fetchJobs(): Promise<JobItem[]> {
  return apiFetch<JobItem[]>("/api/jobs");
}

export async function fetchJob(jobId: string): Promise<JobItem> {
  return apiFetch<JobItem>(`/api/jobs/${jobId}`);
}

export async function updateClipReview(
  jobId: string,
  clipId: string,
  reviewStatus: ReviewStatus
): Promise<ClipItem> {
  return apiFetch<ClipItem>(
    `/api/jobs/${jobId}/clips/${clipId}/review`,
    {
      method: "PATCH",
      body: JSON.stringify({ review_status: reviewStatus }),
    }
  );
}

export async function queueClipUpload(
  jobId: string,
  clipId: string,
  payload: {
    title: string;
    description: string;
    tags: string[];
    privacy: UploadPrivacy;
    scheduled_at?: string | null;
  }
): Promise<UploadItem> {
  return apiFetch<UploadItem>(
    `/api/jobs/${jobId}/clips/${clipId}/upload`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    }
  );
}

