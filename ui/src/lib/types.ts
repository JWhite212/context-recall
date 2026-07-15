/** TypeScript types matching the Python backend models. */

export interface SummaryTemplate {
  name: string;
  description: string;
  system_prompt: string;
  sections: string[];
}

export type DaemonState =
  "idle" | "detecting" | "recording" | "processing" | "unknown";

export type MeetingStatus =
  | "recording"
  | "transcribing"
  | "diarising"
  | "summarising"
  | "writing"
  | "complete"
  | "error"
  | "pending";

export interface Meeting {
  id: string;
  title: string;
  started_at: number;
  ended_at: number | null;
  duration_seconds: number | null;
  status: MeetingStatus;
  audio_path: string | null;
  transcript_json: string | null;
  summary_markdown: string | null;
  tags: string[];
  language: string | null;
  word_count: number | null;
  label: string;
  created_at: number;
  updated_at: number;
  calendar_event_title: string;
  title_source?: string;
  markdown_path?: string;
  attendees_json: string;
  calendar_confidence: number;
  teams_join_url: string;
  teams_meeting_id: string;
  series_id?: string | null;
  notion_page_id?: string;
  client_id?: string | null;
  project_id?: string | null;
  assignment_source?: string;
  assignment_confidence?: number;
  template_name?: string | null;
  template_source?: string | null;
}

/** A client in the client/project store. */
export interface Client {
  id: string;
  name: string;
  description: string;
  aliases: string[];
  email_domains: string[];
  status: "active" | "archived";
  created_at: number;
  updated_at: number;
}

/** A project, optionally belonging to a client. */
export interface Project {
  id: string;
  client_id: string | null;
  name: string;
  description: string;
  aliases: string[];
  status: "active" | "archived";
  created_at: number;
  updated_at: number;
}

export interface CalendarMeetingsResponse {
  meetings: Meeting[];
  count: number;
}

export interface MeetingStats {
  meetings_today: number;
  meetings_this_week: number;
  total_hours: number;
  total_words: number;
  pending_count: number;
  error_count: number;
}

export interface MeetingsResponse {
  meetings: Meeting[];
  total: number;
  limit: number;
  offset: number;
}

export interface StatusResponse {
  state: DaemonState;
  timestamp: number;
  active_meeting?: {
    meeting_id: string | null;
    started_at: number;
    elapsed_seconds: number;
  };
}

export interface HealthResponse {
  status: "ok";
  timestamp: number;
}

export interface TranscriptSegment {
  start: number;
  end: number;
  text: string;
  speaker: string;
}

export interface SpeakerMapping {
  speaker_id: string;
  display_name: string;
  source: string;
  person_id?: string | null;
  confidence?: number | null;
  created_at: number;
}

/** A person in the persistent people directory. */
export interface Person {
  id: string;
  name: string;
  email: string;
  aliases: string[];
  notes: string;
  is_me: boolean;
  sample_count: number;
  created_at: number;
  updated_at: number;
}

/** Metadata for one enrolled voice-profile sample (no embedding blob). */
export interface VoiceSample {
  id: number;
  person_id: string;
  dim: number;
  source_meeting_id: string | null;
  speaker_label: string;
  segment_count: number;
  duration_seconds: number;
  created_at: number;
}

export interface AssignPersonResponse {
  meeting_id: string;
  speaker_id: string;
  person_id: string;
  display_name: string;
  enrolled: boolean;
  reason: string | null;
  sample_count: number;
}

/** Application config sections matching config.yaml. */

export interface DetectionConfig {
  poll_interval_seconds: number;
  min_meeting_duration_seconds: number;
  required_consecutive_detections: number;
  process_names: string[];
}

export interface AudioConfig {
  blackhole_device_name: string;
  mic_device_name: string;
  mic_enabled: boolean;
  mic_volume: number;
  system_volume: number;
  sample_rate: number;
  channels: number;
  temp_audio_dir: string;
  keep_source_files: boolean;
}

export interface TranscriptionConfig {
  model_size: string;
  compute_type: string;
  language: string;
  cpu_threads: number;
  vad_threshold: number;
}

export interface SummarisationConfig {
  backend: "ollama" | "claude";
  anthropic_api_key: string;
  model: string;
  max_tokens: number;
  ollama_base_url: string;
  ollama_model: string;
}

export interface DiarisationConfig {
  enabled: boolean;
  speaker_name: string;
  remote_label: string;
  energy_ratio_threshold: number;
}

export interface MarkdownConfig {
  enabled: boolean;
  vault_path: string;
  filename_template: string;
  include_full_transcript: boolean;
}

export interface NotionConfig {
  enabled: boolean;
  api_key: string;
  database_id: string;
  properties: Record<string, string>;
}

export interface LoggingConfig {
  level: string;
  log_file: string;
}

export interface ApiConfig {
  enabled: boolean;
  host: string;
  port: number;
}

export interface CalendarAttendee {
  name: string;
  email: string;
}

export interface CalendarEvent {
  event_uid: string;
  title: string;
  start_ts: number;
  end_ts: number;
  attendees: CalendarAttendee[];
  organizer: CalendarAttendee | null;
  join_url: string;
  meeting_id: string;
  calendar_name: string;
}

export interface CalendarEventsResponse {
  events: CalendarEvent[];
  count: number;
}

export interface CalendarConfig {
  enabled: boolean;
  time_window_minutes: number;
  min_confidence: number;
  import_enabled: boolean;
  sync_interval_minutes: number;
  sync_horizon_days: number;
  excluded_calendars: string[];
}

export interface AutoArmConfig {
  enabled: boolean;
  lead_minutes: number;
  trailing_minutes: number;
  activity_rms_dbfs: number;
  activity_sustain_seconds: number;
  meeting_process_names: string[];
}

export interface RetentionConfig {
  audio_retention_days: number;
  record_retention_days: number;
}

export interface WebhookChannelConfig {
  enabled: boolean;
  url: string;
  format: string;
}

export interface EmailChannelConfig {
  enabled: boolean;
  smtp_host: string;
  smtp_port: number;
  smtp_user: string;
  smtp_password: string;
  from_address: string;
  to_address: string;
  max_per_day: number;
}

export interface NotificationsConfig {
  enabled: boolean;
  in_app: boolean;
  macos: boolean;
  webhook: WebhookChannelConfig;
  email: EmailChannelConfig;
  default_reminder_before_due: string;
  overdue_check_interval: string;
}

export interface AppConfig {
  detection: DetectionConfig;
  audio: AudioConfig;
  transcription: TranscriptionConfig;
  summarisation: SummarisationConfig;
  diarisation: DiarisationConfig;
  markdown: MarkdownConfig;
  notion: NotionConfig;
  logging: LoggingConfig;
  api: ApiConfig;
  calendar: CalendarConfig;
  auto_arm: AutoArmConfig;
  retention: RetentionConfig;
  notifications: NotificationsConfig;
}

/** Whisper model info from the daemon. */

export interface WhisperModel {
  name: string;
  repo: string;
  size_mb: number;
  status: "downloaded" | "not_downloaded" | "downloading" | "error";
  percent: number;
  error: string | null;
}

export interface ModelsResponse {
  models: WhisperModel[];
}

/** Recording control responses. */

export interface RecordingStartResponse {
  status: "recording";
  started_at: number;
}

export interface RecordingStopResponse {
  status: "stopping" | "deferred";
  meeting_id?: string;
}

/** Audio device info from the daemon. */

export interface AudioDevice {
  index: number;
  name: string;
  channels: number;
  sample_rate: number;
  is_default: boolean;
}

export interface DevicesResponse {
  devices: AudioDevice[];
}

/** Search-related types. */

export interface SearchResult {
  meeting_id: string;
  segment_index: number;
  text: string;
  speaker: string;
  start_time: number;
  score: number;
  meeting_title: string | null;
}

export interface SearchResponse {
  results: SearchResult[];
  query: string;
}

export interface ReindexResponse {
  status: string;
  meetings_indexed: number;
  segments_indexed: number;
}

/** Unresolved pipeline warning surfaced in the live diagnostics banner. */
export interface WarningEvent {
  /** Stable id (typically `source::message`) used for dismissal. */
  id: string;
  /** Source of the warning, e.g. "system", "mic", "audio.xrun", "capture". */
  source: string;
  /** Human-readable hint shown to the user. */
  message: string;
  /** Timestamp (ms since epoch) the UI first saw this warning. */
  createdAt: number;
}

/** WebSocket event types pushed from the daemon. */
export type WSEvent =
  | { type: "meeting.detecting"; consecutive: number; required: number }
  | { type: "meeting.started"; started_at: number }
  | { type: "meeting.ended"; duration: number }
  | { type: "pipeline.stage"; meeting_id: string | null; stage: string }
  | {
      type: "pipeline.progress";
      meeting_id: string | null;
      stage: string;
      percent: number;
    }
  | {
      type: "pipeline.complete";
      meeting_id: string | null;
      title?: string;
      /** True when this completion came from a reprocess, not a live
       *  recording — the UI must never apply a pending live rename to it. */
      is_reprocess?: boolean;
      /** Start timestamp of the recording session that produced this
       *  completion; keys the pending live rename to its own session. */
      started_at?: number;
    }
  | {
      type: "pipeline.error";
      meeting_id: string | null;
      stage: string;
      error: string;
    }
  | {
      type: "pipeline.warning";
      source: string;
      message: string;
      meeting_id?: string | null;
    }
  | {
      type: "transcript.segment";
      meeting_id: string | null;
      segment: TranscriptSegment;
    }
  | { type: "audio.level"; system_rms: number; mic_rms: number }
  | {
      type: "model.download.progress";
      model: string;
      percent: number;
      error?: string;
    }
  | {
      type: "meeting.calendar_match";
      title: string;
      attendees: string[];
      confidence: number;
    }
  | { type: "daemon.status"; state: DaemonState }
  | { type: "meeting.resummarise"; meeting_id: string; status: MeetingStatus }
  | {
      type: "notification";
      notification_type: string;
      title: string;
      body: string;
      reference_id: string | null;
    }
  | { type: "action_items.extracted"; meeting_id: string; count: number }
  | { type: "meeting.renamed"; meeting_id: string; title: string };

/** Action item types. */
export type ActionItemStatus = "open" | "in_progress" | "done" | "cancelled";
export type ActionItemPriority = "low" | "medium" | "high" | "urgent";

export interface ActionItem {
  id: string;
  meeting_id: string;
  title: string;
  description: string | null;
  assignee: string | null;
  status: ActionItemStatus;
  priority: ActionItemPriority;
  due_date: string | null;
  reminder_at: string | null;
  source: "extracted" | "manual";
  extracted_text: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface ActionItemsResponse {
  items: ActionItem[];
}

/** Meeting series types. */
export interface MeetingSeries {
  id: string;
  title: string;
  calendar_series_id: string | null;
  detection_method: "calendar" | "heuristic" | "manual";
  typical_attendees_json: string | null;
  typical_day_of_week: number | null;
  typical_time: string | null;
  typical_duration_minutes: number | null;
  created_at: string;
  updated_at: string;
  meetings?: Meeting[];
}

export interface SeriesListResponse {
  series: MeetingSeries[];
}

export interface SeriesTrends {
  series_id: string;
  meeting_count: number;
  duration_trend: number[];
  word_count_trend: number[];
  avg_duration_minutes: number;
}

/** Analytics types. */
export interface AnalyticsPeriod {
  id: number;
  period_type: string;
  period_start: string;
  total_meetings: number;
  total_duration_minutes: number;
  total_words: number;
  unique_attendees: number;
  recurring_ratio: number;
  action_items_created: number;
  action_items_completed: number;
  busiest_hour: number | null;
  computed_at: number;
}

export interface AnalyticsSummaryResponse {
  current_period: AnalyticsPeriod | null;
  period_type: string;
  period_start: string;
}

export interface AnalyticsTrendsResponse {
  trends: AnalyticsPeriod[];
  period_type: string;
}

export interface LoadScore {
  ratio: number;
  label: string;
  current_minutes: number;
  average_minutes: number;
}

export interface AnalyticsHealthResponse {
  load_score: LoadScore;
  indicators: string[];
}

export interface MostMetPerson {
  name: string;
  meeting_count: number;
}

export interface AnalyticsPeopleResponse {
  people: MostMetPerson[];
}

/** Notification types. */
export type NotificationStatus = "sent" | "dismissed" | "failed";

export interface AppNotification {
  id: string;
  type: string;
  reference_id: string | null;
  channel: string;
  title: string;
  body: string | null;
  status: NotificationStatus;
  scheduled_at: number | null;
  sent_at: number | null;
  created_at: number;
}

export interface NotificationsResponse {
  notifications: AppNotification[];
}

export interface UnreadCountResponse {
  count: number;
}

/** Prep briefing types. */
export interface PrepBriefing {
  id: string;
  meeting_id: string | null;
  series_id: string | null;
  content_markdown: string;
  attendees_json: string;
  related_meeting_ids_json: string;
  open_action_items_json: string;
  generated_at: number;
  expires_at: number;
  calendar_event_uid: string | null;
  event_signature: string | null;
}

export interface PrepGenerateEventBody {
  event_uid: string;
  title: string;
  attendees: { name: string; email: string }[];
  attendee_names: string[];
  end_ts: number;
  series_id?: string | null;
}

/** Ask-your-meetings response. */
export interface AskSource {
  index: number;
  meeting_id: string;
  title: string;
  started_at: number;
  snippet: string;
}

export interface AskResponse {
  answer: string;
  sources: AskSource[];
  no_results: boolean;
}

/** Per-speaker talk-time statistics. */
export interface TalkSpeaker {
  speaker: string;
  seconds: number;
  percent: number;
  turns: number;
  longest_monologue_seconds: number;
}

export interface TalkStats {
  speakers: TalkSpeaker[];
  total_speaking_seconds: number;
}

/** Keyword tracker. */
export interface Tracker {
  id: string;
  name: string;
  keywords: string[];
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export interface TrackerHit {
  id: number;
  tracker_id: string;
  meeting_id: string;
  segment_index: number;
  matched_keyword: string;
  matched_text: string;
  start_time: number;
  created_at: number;
  tracker_name?: string;
  meeting_title?: string;
  meeting_started_at?: number;
}

/** A user-defined insight (LLM extraction) definition. */
export interface InsightDefinition {
  id: string;
  name: string;
  prompt: string;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

/** One extracted insight item on a meeting. */
export interface MeetingInsightResult {
  definition_id: string;
  definition_name: string;
  content: string;
  speaker: string;
}

export type AutomationConditionField =
  "tag" | "client" | "project" | "title_contains" | "attendee_domain";

export interface AutomationCondition {
  field: AutomationConditionField;
  value: string;
}

export type AutomationActionType = "apply_tag" | "webhook" | "notify";

export interface AutomationAction {
  type: AutomationActionType;
  tags?: string[];
  url?: string;
  format?: string;
  message?: string;
}

export interface AutomationRule {
  id: string;
  name: string;
  enabled: boolean;
  match_mode: "all" | "any";
  conditions: AutomationCondition[];
  actions: AutomationAction[];
  created_at: number;
  updated_at: number;
}

/** An automation rule that fired for a meeting. */
export interface MeetingAutomation {
  id: string;
  name: string;
}

export interface EmailDraft {
  subject: string;
  body: string;
}
