import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useRef } from "react";
import Markdown from "react-markdown";
import { getMeeting, deleteMeeting, exportMeeting, resummariseMeeting } from "../../lib/api";
import { API_BASE } from "../../lib/constants";
import type { TranscriptSegment } from "../../lib/types";
import { AudioPlayer, type AudioSeekHandle } from "./AudioPlayer";
import { LoadingBlock } from "../common/Spinner";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function HighlightText({ text, query }: { text: string; query: string }) {
  if (!query) return <>{text}</>;
  const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
  const parts = text.split(regex);
  return (
    <>
      {parts.map((part, i) =>
        regex.test(part) ? (
          <mark key={i} className="bg-accent/30 text-text-primary rounded-sm px-0.5">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

function TranscriptView({
  json,
  onSeek,
}: {
  json: string;
  onSeek?: (seconds: number) => void;
}) {
  const [search, setSearch] = useState("");

  let segments: TranscriptSegment[] = [];
  try {
    const data = JSON.parse(json);
    segments = data.segments ?? [];
  } catch {
    return <p className="text-sm text-text-muted">Unable to parse transcript.</p>;
  }

  if (segments.length === 0) {
    return <p className="text-sm text-text-muted">No transcript segments.</p>;
  }

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
          <button
            key={i}
            onClick={() => onSeek?.(seg.start)}
            className={`flex gap-3 py-1.5 px-1 rounded-md text-left transition-colors ${
              onSeek ? "hover:bg-sidebar-hover cursor-pointer" : ""
            }`}
          >
            <span className="text-[11px] text-text-muted font-mono w-10 shrink-0 pt-0.5">
              {formatTime(seg.start)}
            </span>
            {seg.speaker && (
              <span
                className={`text-[11px] font-medium w-14 shrink-0 pt-0.5 ${
                  seg.speaker === "Me"
                    ? "text-accent"
                    : "text-status-idle"
                }`}
              >
                {seg.speaker}
              </span>
            )}
            <span className="text-sm text-text-primary leading-relaxed">
              <HighlightText text={seg.text} query={search.trim()} />
            </span>
          </button>
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

export function MeetingDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"summary" | "transcript">("summary");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const audioSeekRef = useRef<AudioSeekHandle | null>(null);

  const { data: meeting, isLoading, isError, refetch } = useQuery({
    queryKey: ["meeting", id],
    queryFn: () => getMeeting(id!),
    enabled: !!id,
  });

  const deleteM = useMutation({
    mutationFn: () => deleteMeeting(id!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      navigate("/meetings");
    },
  });

  const resummarise = useMutation({
    mutationFn: () => resummariseMeeting(id!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", id] });
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
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
        <ErrorState message="Failed to load meeting." onRetry={() => refetch()} />
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
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="15 18 9 12 15 6" />
        </svg>
        Back
      </button>

      {/* Header */}
      <div>
        <h1 className="text-lg font-semibold text-text-primary">{meeting.title}</h1>
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
            <span className="text-xs text-text-muted">
              {meeting.language}
            </span>
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
      </div>

      {/* Actions row */}
      <div className="flex items-center gap-2">

      {/* Re-summarise */}
      {hasTranscript && (
        <button
          onClick={() => resummarise.mutate()}
          disabled={resummarise.isPending}
          className="px-3 py-1.5 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover transition-colors flex items-center gap-1.5 disabled:opacity-50"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
          </svg>
          {resummarise.isPending ? "Summarising..." : "Re-summarise"}
        </button>
      )}
      {resummarise.isError && (
        <span className="text-xs text-status-error">
          {(resummarise.error as Error).message}
        </span>
      )}

      {/* Export */}
      <div className="relative inline-block">
        <button
          onClick={() => setExportOpen(!exportOpen)}
          className="px-3 py-1.5 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover transition-colors flex items-center gap-1.5"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Export
        </button>
        {exportOpen && (
          <div className="absolute left-0 mt-1 w-40 rounded-lg bg-surface-raised border border-border shadow-lg z-10 py-1">
            <button
              onClick={async () => {
                setExportOpen(false);
                const md = await exportMeeting(id!, "markdown");
                const blob = new Blob([md], { type: "text/markdown" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `${meeting.title || "meeting"}.md`;
                a.click();
                URL.revokeObjectURL(url);
              }}
              className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
            >
              Markdown (.md)
            </button>
            <button
              onClick={async () => {
                setExportOpen(false);
                const json = await exportMeeting(id!, "json");
                const blob = new Blob([json], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `${meeting.title || "meeting"}.json`;
                a.click();
                URL.revokeObjectURL(url);
              }}
              className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
            >
              JSON (.json)
            </button>
            <button
              onClick={async () => {
                setExportOpen(false);
                const md = meeting.summary_markdown || "";
                await navigator.clipboard.writeText(md);
              }}
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
        <AudioPlayer src={`${API_BASE}/api/meetings/${meeting.id}/audio`} seekRef={audioSeekRef} />
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
                <Markdown>{meeting.summary_markdown!}</Markdown>
              </div>
            ) : activeTab === "transcript" && hasTranscript ? (
              <TranscriptView
                json={meeting.transcript_json!}
                onSeek={hasAudio ? (s) => audioSeekRef.current?.seekTo(s) : undefined}
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
            <span className="text-sm text-status-error">Delete this meeting permanently?</span>
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
