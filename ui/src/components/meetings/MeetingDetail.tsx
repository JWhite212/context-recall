import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import Markdown from "react-markdown";
import { getMeeting, deleteMeeting } from "../../lib/api";
import { API_BASE } from "../../lib/constants";
import type { TranscriptSegment } from "../../lib/types";
import { AudioPlayer } from "./AudioPlayer";

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function TranscriptView({ json }: { json: string }) {
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

  return (
    <div className="flex flex-col gap-1">
      {segments.map((seg, i) => (
        <div key={i} className="flex gap-3 py-1.5 group">
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
            {seg.text}
          </span>
        </div>
      ))}
    </div>
  );
}

export function MeetingDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [activeTab, setActiveTab] = useState<"summary" | "transcript">("summary");
  const [confirmDelete, setConfirmDelete] = useState(false);

  const { data: meeting, isLoading } = useQuery({
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

  if (isLoading) {
    return (
      <div className="p-6">
        <p className="text-sm text-text-muted">Loading...</p>
      </div>
    );
  }

  if (!meeting) {
    return (
      <div className="p-6">
        <p className="text-sm text-text-muted">Meeting not found.</p>
        <button
          onClick={() => navigate("/meetings")}
          className="mt-2 text-sm text-accent hover:underline"
        >
          Back to meetings
        </button>
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

      {/* Audio player */}
      {hasAudio && (
        <AudioPlayer src={`${API_BASE}/api/meetings/${meeting.id}/audio`} />
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
              <TranscriptView json={meeting.transcript_json!} />
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
