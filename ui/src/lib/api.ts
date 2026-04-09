/** API client for communicating with the MeetingMind daemon. */

import type {
  AppConfig,
  DevicesResponse,
  HealthResponse,
  MeetingsResponse,
  Meeting,
  ModelsResponse,
  RecordingStartResponse,
  RecordingStopResponse,
  StatusResponse,
} from "./types";

import { API_BASE } from "./constants";

let authToken: string | null = null;

/** Set the auth token (read from ~/.config/meetingmind/auth_token by the Tauri side). */
export function setAuthToken(token: string) {
  authToken = token;
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options?.headers as Record<string, string>),
  };

  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }

  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers,
  });

  if (!res.ok) {
    throw new Error(`API ${res.status}: ${res.statusText}`);
  }

  return res.json() as Promise<T>;
}

export async function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/api/health");
}

export async function getStatus(): Promise<StatusResponse> {
  return request<StatusResponse>("/api/status");
}

export async function getMeetings(
  limit = 50,
  offset = 0,
  query?: string,
  status?: string,
): Promise<MeetingsResponse> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  return request<MeetingsResponse>(`/api/meetings?${params}`);
}

export async function getMeeting(id: string): Promise<Meeting> {
  return request<Meeting>(`/api/meetings/${encodeURIComponent(id)}`);
}

export async function deleteMeeting(id: string): Promise<void> {
  await request(`/api/meetings/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function getConfig(): Promise<AppConfig> {
  return request<AppConfig>("/api/config");
}

export async function updateConfig(config: Partial<AppConfig>): Promise<AppConfig> {
  return request<AppConfig>("/api/config", {
    method: "PUT",
    body: JSON.stringify(config),
  });
}

export async function startRecording(): Promise<RecordingStartResponse> {
  return request<RecordingStartResponse>("/api/record/start", { method: "POST" });
}

export async function stopRecording(): Promise<RecordingStopResponse> {
  return request<RecordingStopResponse>("/api/record/stop", { method: "POST" });
}

export async function getDevices(): Promise<DevicesResponse> {
  return request<DevicesResponse>("/api/devices");
}

export async function getModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>("/api/models");
}

export async function downloadModel(name: string): Promise<{ status: string }> {
  return request("/api/models/" + encodeURIComponent(name) + "/download", { method: "POST" });
}

export async function resummariseMeeting(
  id: string,
): Promise<{ meeting_id: string; title: string; tags: string[] }> {
  return request(`/api/meetings/${encodeURIComponent(id)}/resummarise`, { method: "POST" });
}

export async function exportMeeting(
  id: string,
  format: "markdown" | "json" = "markdown",
): Promise<string> {
  const headers: Record<string, string> = {};
  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }
  const res = await fetch(
    `${API_BASE}/api/export/${encodeURIComponent(id)}?format=${format}`,
    { method: "POST", headers },
  );
  if (!res.ok) throw new Error(`Export failed: ${res.status}`);
  return res.text();
}
