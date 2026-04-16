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
  ReindexResponse,
  SearchResponse,
  SpeakerMapping,
  StatusResponse,
  SummaryTemplate,
} from "./types";

import { API_BASE } from "./constants";

let authToken: string | null = null;

/** Set the auth token (read from ~/.config/meetingmind/auth_token by the Tauri side). */
export function setAuthToken(token: string) {
  authToken = token;
}

/** Return the current auth token (used by WebSocket to authenticate via query param). */
export function getAuthToken(): string | null {
  return authToken;
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
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  return request<MeetingsResponse>(`/api/meetings?${params}`);
}

export async function getMeeting(id: string): Promise<Meeting> {
  return request<Meeting>(`/api/meetings/${encodeURIComponent(id)}`);
}

export async function deleteMeeting(id: string): Promise<void> {
  await request(`/api/meetings/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function getConfig(): Promise<AppConfig> {
  return request<AppConfig>("/api/config");
}

export async function updateConfig(
  config: Partial<AppConfig>,
): Promise<AppConfig> {
  return request<AppConfig>("/api/config", {
    method: "PUT",
    body: JSON.stringify(config),
  });
}

export async function startRecording(): Promise<RecordingStartResponse> {
  return request<RecordingStartResponse>("/api/record/start", {
    method: "POST",
  });
}

export async function stopRecording(
  defer = false,
): Promise<RecordingStopResponse> {
  const params = defer ? "?defer=true" : "";
  return request<RecordingStopResponse>(`/api/record/stop${params}`, {
    method: "POST",
  });
}

export async function getDevices(): Promise<DevicesResponse> {
  return request<DevicesResponse>("/api/devices");
}

export async function getModels(): Promise<ModelsResponse> {
  return request<ModelsResponse>("/api/models");
}

export async function downloadModel(name: string): Promise<{ status: string }> {
  return request("/api/models/" + encodeURIComponent(name) + "/download", {
    method: "POST",
  });
}

export async function resummariseMeeting(
  id: string,
  templateName?: string,
): Promise<{ meeting_id: string; title: string; tags: string[] }> {
  const params = templateName
    ? `?template_name=${encodeURIComponent(templateName)}`
    : "";
  return request(
    `/api/meetings/${encodeURIComponent(id)}/resummarise${params}`,
    {
      method: "POST",
    },
  );
}

export async function reprocessMeeting(
  id: string,
): Promise<{ meeting_id: string; title: string; status: string }> {
  return request(`/api/meetings/${encodeURIComponent(id)}/reprocess`, {
    method: "POST",
  });
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

export async function mergeMeetings(
  meetingIds: string[],
): Promise<{ meeting_id: string; title: string }> {
  return request("/api/meetings/merge", {
    method: "POST",
    body: JSON.stringify({ meeting_ids: meetingIds }),
  });
}

export async function setMeetingLabel(
  id: string,
  label: string,
): Promise<void> {
  await request(`/api/meetings/${encodeURIComponent(id)}/label`, {
    method: "PATCH",
    body: JSON.stringify({ label }),
  });
}

export async function getMeetingLabels(): Promise<string[]> {
  const data = await request<{ labels: string[] }>("/api/meetings/labels");
  return data.labels;
}

export async function getTemplates(): Promise<SummaryTemplate[]> {
  return request<SummaryTemplate[]>("/api/templates");
}

export async function getTemplate(name: string): Promise<SummaryTemplate> {
  return request<SummaryTemplate>(`/api/templates/${encodeURIComponent(name)}`);
}

export async function saveTemplate(
  template: Omit<SummaryTemplate, never>,
): Promise<SummaryTemplate> {
  return request<SummaryTemplate>("/api/templates", {
    method: "POST",
    body: JSON.stringify(template),
  });
}

export async function deleteTemplate(name: string): Promise<void> {
  await request(`/api/templates/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export async function searchTranscripts(
  query: string,
  limit = 10,
): Promise<SearchResponse> {
  return request<SearchResponse>("/api/search", {
    method: "POST",
    body: JSON.stringify({ query, limit }),
  });
}

export async function reindexMeetings(): Promise<ReindexResponse> {
  return request<ReindexResponse>("/api/search/reindex", {
    method: "POST",
  });
}

export async function getMeetingSpeakers(
  meetingId: string,
): Promise<SpeakerMapping[]> {
  return request<SpeakerMapping[]>(
    `/api/meetings/${encodeURIComponent(meetingId)}/speakers`,
  );
}

export async function setSpeakerName(
  meetingId: string,
  speakerId: string,
  displayName: string,
): Promise<void> {
  await request(
    `/api/meetings/${encodeURIComponent(meetingId)}/speakers/${encodeURIComponent(speakerId)}`,
    {
      method: "PATCH",
      body: JSON.stringify({ display_name: displayName }),
    },
  );
}
