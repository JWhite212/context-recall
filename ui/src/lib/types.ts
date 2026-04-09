/** TypeScript types matching the Python backend models. */

export type DaemonState = "idle" | "detecting" | "recording" | "processing" | "unknown";

export type MeetingStatus =
  | "recording"
  | "transcribing"
  | "diarising"
  | "summarising"
  | "writing"
  | "complete"
  | "error";

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
  created_at: number;
  updated_at: number;
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

export interface RetentionConfig {
  audio_retention_days: number;
  record_retention_days: number;
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
  retention: RetentionConfig;
}

/** Whisper model info from the daemon. */

export interface WhisperModel {
  name: string;
  repo: string;
  size_mb: number;
  status: "downloaded" | "not_downloaded" | "downloading" | "error";
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
  status: "stopping";
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

/** WebSocket event types pushed from the daemon. */
export type WSEvent =
  | { type: "meeting.detecting"; consecutive: number; required: number }
  | { type: "meeting.started"; started_at: number }
  | { type: "meeting.ended"; duration: number }
  | { type: "pipeline.stage"; meeting_id: string | null; stage: string }
  | { type: "pipeline.progress"; meeting_id: string | null; stage: string; percent: number }
  | { type: "pipeline.complete"; meeting_id: string | null; title?: string }
  | { type: "pipeline.error"; meeting_id: string | null; stage: string; error: string }
  | { type: "transcript.segment"; meeting_id: string | null; segment: TranscriptSegment }
  | { type: "audio.level"; system_rms: number; mic_rms: number }
  | { type: "daemon.status"; state: DaemonState };
