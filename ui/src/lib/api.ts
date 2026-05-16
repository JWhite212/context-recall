/** API client for communicating with the Context Recall daemon. */

import { invoke } from "@tauri-apps/api/core";

import type {
  ActionItem,
  ActionItemsResponse,
  AnalyticsHealthResponse,
  AnalyticsPeopleResponse,
  AnalyticsSummaryResponse,
  AnalyticsTrendsResponse,
  AppConfig,
  CalendarMeetingsResponse,
  DevicesResponse,
  HealthResponse,
  MeetingSeries,
  MeetingStats,
  MeetingsResponse,
  Meeting,
  ModelsResponse,
  NotificationsResponse,
  PrepBriefing,
  RecordingStartResponse,
  RecordingStopResponse,
  ReindexResponse,
  SearchResponse,
  SeriesListResponse,
  SeriesTrends,
  SpeakerMapping,
  StatusResponse,
  SummaryTemplate,
  UnreadCountResponse,
} from "./types";

import { API_BASE } from "./constants";

let authToken: string | null = null;
const tokenSubscribers = new Set<(token: string | null) => void>();

/** Set the auth token (read from ~/Library/Application Support/Context Recall/auth_token by the Tauri side). */
export function setAuthToken(token: string | null) {
  if (authToken === token) return;
  authToken = token;
  for (const sub of tokenSubscribers) {
    try {
      sub(token);
    } catch {
      // Subscribers must not throw; swallow to keep the rest notified.
    }
  }
}

/**
 * Return the current bearer token, or null if none is set.
 *
 * Used by the WebSocket as the first message after the connection opens
 * (`{ type: "auth", token }`), and by HTTP requests as the
 * `Authorization: Bearer <token>` header.
 */
export function getAuthToken(): string | null {
  return authToken;
}

/**
 * Subscribe to auth-token changes. The subscriber fires whenever
 * `setAuthToken` is called with a value different from the previous token
 * (including null on logout/rotation). Returns an unsubscribe function.
 */
export function subscribeAuthToken(
  subscriber: (token: string | null) => void,
): () => void {
  tokenSubscribers.add(subscriber);
  return () => {
    tokenSubscribers.delete(subscriber);
  };
}

/** Default per-request timeout (ms). Callers may override via RequestOptions. */
export const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Structured error thrown by `request<T>` (and the export/prep helpers that
 * share its contract). The UI catches this via `toastApiError` to surface a
 * consistent toast message.
 *
 * - `status` is 0 for network / abort failures, otherwise the HTTP status.
 * - `detail` is the server-supplied detail / error / message, or a generic
 *   description for non-HTTP failures.
 * - `retried` is true when the request was retried once after a 401 with a
 *   refreshed token (still ended up failing).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  readonly retried: boolean;

  constructor(status: number, detail: string, retried = false) {
    super(`API ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.retried = retried;
  }
}

export interface RequestOptions extends RequestInit {
  /** Override the default 30s timeout. Set to 0 to disable. */
  timeoutMs?: number;
}

/** Extract a human-readable detail string from a fetch Response. */
async function extractDetail(res: Response): Promise<string> {
  let detail = res.statusText;
  try {
    const body = await res.json();
    if (Array.isArray(body.detail)) {
      detail = body.detail
        .map((e: { msg?: string }) => e.msg ?? JSON.stringify(e))
        .join("; ");
    } else {
      detail = body.detail || body.error || body.message || detail;
    }
  } catch {
    // Body was empty or not JSON — keep the statusText fallback.
  }
  return detail;
}

/**
 * Core fetch helper: applies auth, timeout/abort, and the once-only 401
 * retry, then returns the raw `Response`. Throws `ApiError` on non-OK
 * statuses and on abort/timeout/network failure.
 *
 * - 30s default timeout via `AbortSignal.timeout`; per-call override via
 *   `timeoutMs`, or 0 to disable.
 * - Caller-supplied `signal` is composed with the timeout via
 *   `AbortSignal.any` when available.
 * - On 401, re-reads the token via the `read_auth_token` Tauri command
 *   and retries once. A second 401 surfaces as `ApiError` with
 *   `retried: true`.
 */
async function fetchWithContract(
  path: string,
  options: RequestOptions | undefined,
  extraHeaders: Record<string, string>,
  retried: boolean,
): Promise<Response> {
  const { timeoutMs, signal: callerSignal, ...rest } = options ?? {};

  const headers: Record<string, string> = {
    ...extraHeaders,
    ...(rest.headers as Record<string, string>),
  };
  if (authToken) {
    headers["Authorization"] = `Bearer ${authToken}`;
  }

  const effectiveTimeout = timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const signal = buildSignal(callerSignal, effectiveTimeout);

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, { ...rest, headers, signal });
  } catch (e) {
    const name = (e as { name?: string })?.name;
    if (name === "AbortError" || name === "TimeoutError") {
      throw new ApiError(
        0,
        `Request timed out after ${effectiveTimeout}ms`,
        retried,
      );
    }
    const message = e instanceof Error ? e.message : String(e);
    throw new ApiError(0, message, retried);
  }

  if (res.status === 401 && !retried) {
    try {
      const fresh = (await invoke("read_auth_token")) as string | null;
      if (fresh && fresh.length > 0 && fresh !== authToken) {
        authToken = fresh;
      }
    } catch {
      // Fall through and retry even if the Tauri command fails so a
      // transient 401 isn't masked.
    }
    return fetchWithContract(path, options, extraHeaders, true);
  }

  if (!res.ok) {
    throw new ApiError(res.status, await extractDetail(res), retried);
  }

  return res;
}

/**
 * Perform an authenticated JSON request against the daemon API and parse
 * the response body as `T`. See `fetchWithContract` for the timeout/abort/
 * 401-retry/error contract.
 */
async function request<T>(path: string, options?: RequestOptions): Promise<T> {
  const res = await fetchWithContract(
    path,
    options,
    { "Content-Type": "application/json" },
    false,
  );
  return res.json() as Promise<T>;
}

/**
 * Low-level variant of `request<T>` that returns the raw `Response`. Used
 * by helpers that need the body as text (exports) or need to distinguish
 * 204 No Content from a JSON body (prep briefings).
 */
async function requestRaw(
  path: string,
  options?: RequestOptions,
): Promise<Response> {
  return fetchWithContract(path, options, {}, false);
}

/**
 * Compose the caller's AbortSignal (if any) with a timeout signal so the
 * fetch aborts on whichever fires first. Falls back gracefully when
 * `AbortSignal.any` isn't available.
 */
function buildSignal(
  caller: AbortSignal | null | undefined,
  timeoutMs: number,
): AbortSignal | undefined {
  if (timeoutMs <= 0) return caller ?? undefined;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  if (!caller) return timeoutSignal;
  // AbortSignal.any landed in Node 20.3 / modern browsers. Fall back to the
  // caller signal if it isn't present so we still honour the caller's intent.
  const any = (
    AbortSignal as unknown as {
      any?: (signals: AbortSignal[]) => AbortSignal;
    }
  ).any;
  return any ? any([caller, timeoutSignal]) : caller;
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
  tag?: string,
  sort?: string,
): Promise<MeetingsResponse> {
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (query) params.set("q", query);
  if (status) params.set("status", status);
  if (tag) params.set("tag", tag);
  if (sort) params.set("sort", sort);
  return request<MeetingsResponse>(`/api/meetings?${params}`);
}

export async function getMeetingStats(): Promise<MeetingStats> {
  return request<MeetingStats>("/api/meetings/stats");
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
): Promise<{ meeting_id: string; status: string }> {
  return request(`/api/meetings/${encodeURIComponent(id)}/reprocess`, {
    method: "POST",
  });
}

export async function exportMeeting(
  id: string,
  format: "markdown" | "json" = "markdown",
): Promise<string> {
  const res = await requestRaw(
    `/api/export/${encodeURIComponent(id)}?format=${format}`,
    { method: "POST" },
  );
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
  template: SummaryTemplate,
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

export async function getCalendarMeetings(
  start: number,
  end: number,
): Promise<CalendarMeetingsResponse> {
  return request(`/api/calendar/meetings?start=${start}&end=${end}`);
}

// --- Action Items ---

export async function getActionItems(
  status?: string,
  assignee?: string,
  limit = 100,
): Promise<ActionItemsResponse> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (status) params.set("status", status);
  if (assignee) params.set("assignee", assignee);
  return request<ActionItemsResponse>(`/api/action-items?${params}`);
}

export async function getMeetingActionItems(
  meetingId: string,
): Promise<ActionItemsResponse> {
  return request<ActionItemsResponse>(
    `/api/meetings/${encodeURIComponent(meetingId)}/action-items`,
  );
}

export async function createActionItem(data: {
  meeting_id: string;
  title: string;
  assignee?: string;
  priority?: string;
  due_date?: string;
  description?: string;
}): Promise<ActionItem> {
  return request<ActionItem>("/api/action-items", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function updateActionItem(
  id: string,
  data: Partial<ActionItem>,
): Promise<ActionItem> {
  return request<ActionItem>(`/api/action-items/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify(data),
  });
}

export async function deleteActionItem(id: string): Promise<void> {
  await request(`/api/action-items/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

// --- Series ---

export async function getSeries(): Promise<SeriesListResponse> {
  return request<SeriesListResponse>("/api/series");
}

export async function getSeriesDetail(id: string): Promise<MeetingSeries> {
  return request<MeetingSeries>(`/api/series/${encodeURIComponent(id)}`);
}

export async function createSeries(title: string): Promise<MeetingSeries> {
  return request<MeetingSeries>("/api/series", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export async function deleteSeries(id: string): Promise<void> {
  await request(`/api/series/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function linkMeetingToSeries(
  seriesId: string,
  meetingId: string,
): Promise<void> {
  await request(`/api/series/${encodeURIComponent(seriesId)}/meetings`, {
    method: "POST",
    body: JSON.stringify({ meeting_id: meetingId }),
  });
}

export async function getSeriesTrends(id: string): Promise<SeriesTrends> {
  return request<SeriesTrends>(`/api/series/${encodeURIComponent(id)}/trends`);
}

// --- Analytics ---

export async function getAnalyticsSummary(
  period = "weekly",
): Promise<AnalyticsSummaryResponse> {
  return request<AnalyticsSummaryResponse>(
    `/api/analytics/summary?period=${period}`,
  );
}

export async function getAnalyticsTrends(
  periodType = "weekly",
  weeks = 8,
): Promise<AnalyticsTrendsResponse> {
  const params = new URLSearchParams({
    period_type: periodType,
    weeks: String(weeks),
  });
  return request<AnalyticsTrendsResponse>(`/api/analytics/trends?${params}`);
}

export async function getAnalyticsPeople(
  limit = 10,
): Promise<AnalyticsPeopleResponse> {
  return request<AnalyticsPeopleResponse>(
    `/api/analytics/people?limit=${limit}`,
  );
}

export async function getAnalyticsHealth(): Promise<AnalyticsHealthResponse> {
  return request<AnalyticsHealthResponse>("/api/analytics/health");
}

export async function refreshAnalytics(): Promise<void> {
  await request("/api/analytics/refresh", { method: "POST" });
}

// --- Notifications ---

export async function getNotifications(
  limit = 50,
): Promise<NotificationsResponse> {
  return request<NotificationsResponse>(`/api/notifications?limit=${limit}`);
}

export async function getUnreadCount(): Promise<UnreadCountResponse> {
  return request<UnreadCountResponse>("/api/notifications/unread-count");
}

export async function dismissNotification(id: string): Promise<void> {
  await request(`/api/notifications/${encodeURIComponent(id)}`, {
    method: "PATCH",
    body: JSON.stringify({ status: "dismissed" }),
  });
}

// --- Prep Briefings ---

export async function getUpcomingPrep(): Promise<PrepBriefing | null> {
  const res = await requestRaw("/api/prep/upcoming");
  if (res.status === 204) return null;
  return res.json() as Promise<PrepBriefing>;
}

export async function getPrepForMeeting(
  meetingId: string,
): Promise<PrepBriefing> {
  return request<PrepBriefing>(`/api/prep/${encodeURIComponent(meetingId)}`);
}

export async function generatePrep(meetingId: string): Promise<PrepBriefing> {
  return request<PrepBriefing>(
    `/api/prep/${encodeURIComponent(meetingId)}/generate`,
    { method: "POST" },
  );
}
