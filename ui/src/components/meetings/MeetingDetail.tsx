import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useRef, useEffect } from "react";
import Markdown from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import {
  getMeeting,
  deleteMeeting,
  exportMeeting,
  resummariseMeeting,
  setMeetingLabel,
  getMeetingLabels,
  getTemplates,
  setSpeakerName,
  reprocessMeeting,
} from "../../lib/api";
import { API_BASE } from "../../lib/constants";
import type { TranscriptSegment } from "../../lib/types";
import { AudioPlayer, type AudioSeekHandle } from "./AudioPlayer";
import { LoadingBlock } from "../common/Spinner";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { useToast } from "../common/Toast";

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function HighlightText({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`(${escaped})`, "gi");
  const parts = text.split(regex);
  const lowerQuery = query.toLowerCase();
  return (
    <>
      {parts.map((part, i) =>
        part.toLowerCase() === lowerQuery ? (
          <mark
            key={i}
            className="bg-accent/30 text-text-primary rounded-sm px-0.5"
          >
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

const SPEAKER_COLORS = [
  "text-blue-400",
  "text-emerald-400",
  "text-amber-400",
  "text-purple-400",
  "text-rose-400",
  "text-cyan-400",
  "text-orange-400",
  "text-lime-400",
];

function getSpeakerColor(speaker: string, speakerList: string[]): string {
  const idx = speakerList.indexOf(speaker);
  return SPEAKER_COLORS[idx % SPEAKER_COLORS.length] || SPEAKER_COLORS[0];
}

function SpeakerLabel({
  speaker,
  meetingId,
  color,
  onRenamed,
}: {
  speaker: string;
  meetingId: string;
  color: string;
  onRenamed: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(speaker);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const handleSubmit = async () => {
    const trimmed = name.trim();
    if (trimmed && trimmed !== speaker) {
      try {
        await setSpeakerName(meetingId, speaker, trimmed);
        onRenamed();
      } catch {
        setName(speaker); // revert on error
      }
    }
    setEditing(false);
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={handleSubmit}
        onKeyDown={(e) => {
          if (e.key === "Enter") handleSubmit();
          if (e.key === "Escape") {
            setName(speaker);
            setEditing(false);
          }
        }}
        className="px-1 py-0.5 text-xs rounded bg-surface border border-border text-text-primary w-24"
      />
    );
  }

  return (
    <button
      onClick={() => setEditing(true)}
      className={`${color} text-xs font-medium hover:underline cursor-pointer`}
      title="Click to rename speaker"
    >
      [{speaker}]
    </button>
  );
}

function TranscriptView({
  json,
  meetingId,
  onSeek,
  onRenamed,
}: {
  json: string;
  meetingId: string;
  onSeek?: (seconds: number) => void;
  onRenamed: () => void;
}) {
  const [search, setSearch] = useState("");

  let segments: TranscriptSegment[] = [];
  try {
    const data = JSON.parse(json);
    segments = (data.segments ?? []).filter((s: TranscriptSegment) =>
      s.text?.trim(),
    );
  } catch {
    return (
      <p className="text-sm text-text-muted">Unable to parse transcript.</p>
    );
  }

  if (segments.length === 0) {
    return <p className="text-sm text-text-muted">No transcript segments.</p>;
  }

  const uniqueSpeakers = Array.from(
    new Set(segments.map((s) => s.speaker).filter(Boolean)),
  );

  const query = search.trim().toLowerCase();
  const filtered = query
    ? segments.filter((s) => s.text.toLowerCase().includes(query))
    : segments;

  return (
    <div className="flex flex-col gap-3">
      {/* Search */}
      <div className="flex items-center gap-2">
        <input
          type="text"
          placeholder="Search transcript..."
          aria-label="Search transcript"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 px-3 py-1.5 text-sm rounded-lg bg-surface border border-border text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-accent"
        />
        {query && (
          <span className="text-xs text-text-muted shrink-0">
            {filtered.length} / {segments.length}
          </span>
        )}
      </div>

      {/* Segments */}
      <div className="flex flex-col gap-0.5">
        {filtered.map((seg, i) => (
          <div
            key={i}
            onClick={() => onSeek?.(seg.start)}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") onSeek?.(seg.start);
            }}
            className={`flex gap-3 py-1.5 px-1 rounded-md text-left transition-colors ${
              onSeek ? "hover:bg-sidebar-hover cursor-pointer" : ""
            }`}
          >
            <span className="text-[11px] text-text-muted font-mono w-10 shrink-0 pt-0.5">
              {formatTime(seg.start)}
            </span>
            {seg.speaker && (
              <span
                className="w-14 shrink-0 pt-0.5"
                onClick={(e) => e.stopPropagation()}
                onKeyDown={(e) => e.stopPropagation()}
                role="presentation"
              >
                <SpeakerLabel
                  speaker={seg.speaker}
                  meetingId={meetingId}
                  color={getSpeakerColor(seg.speaker, uniqueSpeakers)}
                  onRenamed={onRenamed}
                />
              </span>
            )}
            <span className="text-sm text-text-primary leading-relaxed">
              <HighlightText text={seg.text} query={search.trim()} />
            </span>
          </div>
        ))}
        {query && filtered.length === 0 && (
          <p className="text-xs text-text-muted py-4 text-center">
            No segments match "{search}".
          </p>
        )}
      </div>
    </div>
  );
}

function LabelEditor({
  meetingId,
  initialLabel,
}: {
  meetingId: string;
  initialLabel: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [value, setValue] = useState(initialLabel);
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const { data: labels = [] } = useQuery({
    queryKey: ["meeting-labels"],
    queryFn: getMeetingLabels,
    staleTime: 30_000,
  });

  const saveLabel = useMutation({
    mutationFn: (label: string) => setMeetingLabel(meetingId, label),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      queryClient.invalidateQueries({ queryKey: ["meeting-labels"] });
    },
    onError: () => {
      toast.error("Failed to save label.");
    },
  });

  const commit = () => {
    const trimmed = value.trim();
    if (trimmed !== initialLabel) {
      saveLabel.mutate(trimmed);
    }
    setOpen(false);
  };

  const filtered = labels.filter(
    (l) => l.toLowerCase().includes(value.toLowerCase()) && l !== value,
  );

  // Close dropdown on click outside.
  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        commit();
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  });

  return (
    <div className="relative inline-block" ref={wrapperRef}>
      <input
        type="text"
        value={value}
        onChange={(e) => {
          setValue(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => {
          // Delay to allow dropdown click to fire first.
          setTimeout(() => commit(), 150);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
            (e.target as HTMLInputElement).blur();
          }
          if (e.key === "Escape") {
            setValue(initialLabel);
            setOpen(false);
            (e.target as HTMLInputElement).blur();
          }
        }}
        placeholder="Add label..."
        aria-label="Meeting label"
        className="px-2 py-1 text-xs rounded-md bg-surface-raised border border-border text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-purple-400 w-40"
      />
      {open && filtered.length > 0 && (
        <div className="absolute left-0 mt-1 w-40 rounded-lg bg-surface-raised border border-border shadow-lg z-10 py-1 max-h-32 overflow-y-auto">
          {filtered.map((label) => (
            <button
              key={label}
              onMouseDown={(e) => {
                e.preventDefault();
                setValue(label);
                saveLabel.mutate(label);
                setOpen(false);
              }}
              className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
            >
              {label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function MeetingDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"summary" | "transcript">(
    "summary",
  );
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [resummariseOpen, setResummariseOpen] = useState(false);
  const [selectedTemplate, setSelectedTemplate] = useState("standard");
  const exportMenuRef = useRef<HTMLDivElement>(null);
  const resummariseMenuRef = useRef<HTMLDivElement>(null);
  const audioSeekRef = useRef<AudioSeekHandle | null>(null);

  const { data: templates = [] } = useQuery({
    queryKey: ["templates"],
    queryFn: getTemplates,
    staleTime: 60_000,
  });

  // Close export dropdown on Escape or click outside.
  useEffect(() => {
    if (!exportOpen) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setExportOpen(false);
    };
    const handleClick = (e: MouseEvent) => {
      if (
        exportMenuRef.current &&
        !exportMenuRef.current.contains(e.target as Node)
      ) {
        setExportOpen(false);
      }
    };
    document.addEventListener("keydown", handleKey);
    document.addEventListener("mousedown", handleClick);
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.removeEventListener("mousedown", handleClick);
    };
  }, [exportOpen]);

  // Close re-summarise popover on Escape or click outside.
  useEffect(() => {
    if (!resummariseOpen) return;
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setResummariseOpen(false);
    };
    const handleClick = (e: MouseEvent) => {
      if (
        resummariseMenuRef.current &&
        !resummariseMenuRef.current.contains(e.target as Node)
      ) {
        setResummariseOpen(false);
      }
    };
    document.addEventListener("keydown", handleKey);
    document.addEventListener("mousedown", handleClick);
    return () => {
      document.removeEventListener("keydown", handleKey);
      document.removeEventListener("mousedown", handleClick);
    };
  }, [resummariseOpen]);

  const {
    data: meeting,
    isLoading,
    isError,
    refetch,
  } = useQuery({
    queryKey: ["meeting", id],
    queryFn: () => getMeeting(id!),
    enabled: !!id,
  });

  const toast = useToast();

  const deleteM = useMutation({
    mutationFn: () => deleteMeeting(id!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      toast.success("Meeting deleted.");
      navigate("/meetings");
    },
  });

  const resummarise = useMutation({
    mutationFn: (templateName?: string) =>
      resummariseMeeting(id!, templateName),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", id] });
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      setResummariseOpen(false);
    },
  });

  const reprocess = useMutation({
    mutationFn: () => reprocessMeeting(id!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", id] });
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      toast.success("Meeting reprocessed successfully.");
    },
    onError: (err) => {
      queryClient.invalidateQueries({ queryKey: ["meeting", id] });
      toast.error(err instanceof Error ? err.message : "Reprocessing failed");
    },
  });

  if (isLoading) {
    return (
      <div className="p-6">
        <LoadingBlock label="Loading meeting..." />
      </div>
    );
  }

  if (isError) {
    return (
      <div className="p-6 max-w-3xl">
        <ErrorState
          message="Failed to load meeting."
          onRetry={() => refetch()}
        />
      </div>
    );
  }

  if (!meeting) {
    return (
      <div className="p-6 max-w-3xl">
        <EmptyState
          title="Meeting not found"
          description="This meeting may have been deleted."
          action={
            <button
              onClick={() => navigate("/meetings")}
              className="px-4 py-1.5 text-sm rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
            >
              Back to meetings
            </button>
          }
        />
      </div>
    );
  }

  const hasTranscript = !!meeting.transcript_json;
  const hasSummary = !!meeting.summary_markdown;
  const hasAudio = !!meeting.audio_path;

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      {/* Back button */}
      <button
        onClick={() => navigate("/meetings")}
        className="text-sm text-text-secondary hover:text-text-primary w-fit flex items-center gap-1"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <polyline points="15 18 9 12 15 6" />
        </svg>
        Back
      </button>

      {/* Header */}
      <div>
        <h1 className="text-lg font-semibold text-text-primary">
          {meeting.title}
        </h1>
        <div className="flex items-center gap-3 mt-1 flex-wrap">
          <span className="text-xs text-text-muted">
            {new Date(meeting.started_at * 1000).toLocaleDateString(undefined, {
              weekday: "short",
              month: "short",
              day: "numeric",
              year: "numeric",
              hour: "numeric",
              minute: "2-digit",
            })}
          </span>
          {meeting.duration_seconds != null && (
            <span className="text-xs text-text-muted">
              {Math.round(meeting.duration_seconds / 60)}m
            </span>
          )}
          {meeting.language && (
            <span className="text-xs text-text-muted">{meeting.language}</span>
          )}
          {meeting.word_count != null && (
            <span className="text-xs text-text-muted">
              {meeting.word_count.toLocaleString()} words
            </span>
          )}
          <span
            className={`text-xs px-2 py-0.5 rounded-full ${
              meeting.status === "complete"
                ? "bg-status-idle/20 text-status-idle"
                : meeting.status === "error"
                  ? "bg-status-error/20 text-status-error"
                  : "bg-blue-400/20 text-blue-400"
            }`}
          >
            {meeting.status}
          </span>
        </div>

        {/* Tags */}
        {meeting.tags.length > 0 && (
          <div className="flex gap-1.5 mt-2">
            {meeting.tags.map((tag) => (
              <span
                key={tag}
                className="text-[11px] px-2 py-0.5 rounded-full bg-accent/10 text-accent"
              >
                {tag}
              </span>
            ))}
          </div>
        )}

        {/* Label */}
        <div className="mt-2">
          <LabelEditor meetingId={meeting.id} initialLabel={meeting.label} />
        </div>
      </div>

      {/* Actions row */}
      <div className="flex items-center gap-2">
        {/* Process / retry for pending or error meetings */}
        {(meeting.status === "error" || meeting.status === "pending") &&
          meeting.audio_path && (
            <button
              onClick={() => reprocess.mutate()}
              disabled={reprocess.isPending}
              className="px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent/90 transition-colors flex items-center gap-1.5 disabled:opacity-50"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <polyline points="23 4 23 10 17 10" />
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
              {reprocess.isPending
                ? "Processing..."
                : meeting.status === "pending"
                  ? "Process Now"
                  : "Retry Transcription"}
            </button>
          )}
        {/* Re-summarise */}
        {hasTranscript && (
          <div className="relative inline-block" ref={resummariseMenuRef}>
            <button
              onClick={() => setResummariseOpen(!resummariseOpen)}
              disabled={resummarise.isPending}
              className="px-3 py-1.5 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover transition-colors flex items-center gap-1.5 disabled:opacity-50"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <polyline points="23 4 23 10 17 10" />
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
              </svg>
              {resummarise.isPending ? "Summarising..." : "Re-summarise"}
            </button>
            {resummariseOpen && (
              <div className="absolute left-0 mt-1 w-56 rounded-lg bg-surface-raised border border-border shadow-lg z-10 p-3 flex flex-col gap-2">
                <label className="text-xs text-text-muted">Template</label>
                <select
                  value={selectedTemplate}
                  onChange={(e) => setSelectedTemplate(e.target.value)}
                  className="w-full bg-surface border border-border rounded-lg px-2 py-1.5 text-sm text-text-primary focus:outline-none focus:border-accent appearance-none cursor-pointer"
                >
                  {templates.map((t) => (
                    <option key={t.name} value={t.name}>
                      {t.name}
                    </option>
                  ))}
                </select>
                <div className="flex gap-2 justify-end mt-1">
                  <button
                    onClick={() => setResummariseOpen(false)}
                    className="px-2 py-1 text-xs rounded-lg bg-surface border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={() => resummarise.mutate(selectedTemplate)}
                    disabled={resummarise.isPending}
                    className="px-2 py-1 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
                  >
                    {resummarise.isPending ? "Summarising..." : "Re-summarise"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
        {resummarise.isError && (
          <span className="text-xs text-status-error">
            {resummarise.error instanceof Error
              ? resummarise.error.message
              : "An unexpected error occurred"}
          </span>
        )}

        {/* Export */}
        <div className="relative inline-block" ref={exportMenuRef}>
          <button
            onClick={() => setExportOpen(!exportOpen)}
            aria-haspopup="true"
            aria-expanded={exportOpen}
            className="px-3 py-1.5 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover transition-colors flex items-center gap-1.5"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
              <polyline points="7 10 12 15 17 10" />
              <line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            Export
          </button>
          {exportOpen && (
            <div
              role="menu"
              className="absolute left-0 mt-1 w-40 rounded-lg bg-surface-raised border border-border shadow-lg z-10 py-1"
            >
              <button
                onClick={async () => {
                  setExportOpen(false);
                  try {
                    const md = await exportMeeting(id!, "markdown");
                    const blob = new Blob([md], { type: "text/markdown" });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = `${meeting.title || "meeting"}.md`;
                    a.click();
                    URL.revokeObjectURL(url);
                    toast.success("Exported as Markdown.");
                  } catch {
                    toast.error("Failed to export Markdown.");
                  }
                }}
                role="menuitem"
                className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
              >
                Markdown (.md)
              </button>
              <button
                onClick={async () => {
                  setExportOpen(false);
                  try {
                    const json = await exportMeeting(id!, "json");
                    const blob = new Blob([json], { type: "application/json" });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = `${meeting.title || "meeting"}.json`;
                    a.click();
                    URL.revokeObjectURL(url);
                    toast.success("Exported as JSON.");
                  } catch {
                    toast.error("Failed to export JSON.");
                  }
                }}
                role="menuitem"
                className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
              >
                JSON (.json)
              </button>
              <button
                onClick={async () => {
                  setExportOpen(false);
                  const md = meeting.summary_markdown || "";
                  await navigator.clipboard.writeText(md);
                  toast.success("Summary copied to clipboard.");
                }}
                role="menuitem"
                className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
              >
                Copy summary
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Audio player */}
      {hasAudio && (
        <AudioPlayer
          src={`${API_BASE}/api/meetings/${meeting.id}/audio`}
          seekRef={audioSeekRef}
        />
      )}

      {/* Tabs */}
      {(hasSummary || hasTranscript) && (
        <>
          <div className="flex gap-1 border-b border-border">
            {hasSummary && (
              <button
                onClick={() => setActiveTab("summary")}
                className={`px-4 py-2 text-sm transition-colors border-b-2 -mb-px ${
                  activeTab === "summary"
                    ? "border-accent text-text-primary"
                    : "border-transparent text-text-secondary hover:text-text-primary"
                }`}
              >
                Summary
              </button>
            )}
            {hasTranscript && (
              <button
                onClick={() => setActiveTab("transcript")}
                className={`px-4 py-2 text-sm transition-colors border-b-2 -mb-px ${
                  activeTab === "transcript"
                    ? "border-accent text-text-primary"
                    : "border-transparent text-text-secondary hover:text-text-primary"
                }`}
              >
                Transcript
              </button>
            )}
          </div>

          <div className="rounded-xl bg-surface-raised border border-border p-6 max-h-[60vh] overflow-y-auto">
            {activeTab === "summary" && hasSummary ? (
              <div className="prose prose-sm prose-invert max-w-none text-text-primary [&_h1]:text-text-primary [&_h2]:text-text-primary [&_h3]:text-text-primary [&_li]:text-text-primary [&_p]:text-text-secondary [&_strong]:text-text-primary">
                <Markdown
                  rehypePlugins={[
                    [
                      rehypeSanitize,
                      {
                        ...defaultSchema,
                        attributes: {
                          ...defaultSchema.attributes,
                          a: [
                            ...(defaultSchema.attributes?.a || []),
                            "className",
                          ],
                        },
                      },
                    ],
                  ]}
                  components={{
                    a: ({ href, children, ...props }) => {
                      const safeHref =
                        href && /^(https?:|mailto:|#)/i.test(href)
                          ? href
                          : undefined;
                      return (
                        <a href={safeHref} rel="noopener noreferrer" {...props}>
                          {children}
                        </a>
                      );
                    },
                  }}
                >
                  {meeting.summary_markdown!}
                </Markdown>
              </div>
            ) : activeTab === "transcript" && hasTranscript ? (
              <TranscriptView
                json={meeting.transcript_json!}
                meetingId={meeting.id}
                onSeek={
                  hasAudio ? (s) => audioSeekRef.current?.seekTo(s) : undefined
                }
                onRenamed={() =>
                  queryClient.invalidateQueries({
                    queryKey: ["meeting", id],
                  })
                }
              />
            ) : (
              <p className="text-sm text-text-muted">No content available.</p>
            )}
          </div>
        </>
      )}

      {/* Danger zone */}
      <div className="pt-4 border-t border-border">
        {confirmDelete ? (
          <div className="flex items-center gap-3">
            <span className="text-sm text-status-error">
              Delete this meeting permanently?
            </span>
            <button
              onClick={() => deleteM.mutate()}
              disabled={deleteM.isPending}
              className="px-3 py-1 text-xs rounded-lg bg-status-error text-white hover:opacity-90 disabled:opacity-50"
            >
              {deleteM.isPending ? "Deleting..." : "Yes, delete"}
            </button>
            <button
              onClick={() => setConfirmDelete(false)}
              className="px-3 py-1 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={() => setConfirmDelete(true)}
            className="text-xs text-text-muted hover:text-status-error transition-colors"
          >
            Delete meeting
          </button>
        )}
      </div>
    </div>
  );
}
