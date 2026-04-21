import { useNavigate } from "react-router-dom";
import type { Meeting } from "../../lib/types";

const STATUS_COLORS: Record<string, string> = {
  complete: "bg-status-idle",
  recording: "bg-status-recording",
  error: "bg-status-error",
  pending: "bg-amber-400",
};

interface EventCardProps {
  meeting: Meeting;
  compact?: boolean;
}

export function EventCard({ meeting, compact = false }: EventCardProps) {
  const navigate = useNavigate();
  const title = meeting.title || "Untitled";
  const durationMin = meeting.duration_seconds
    ? Math.round(meeting.duration_seconds / 60)
    : null;
  const statusColor = STATUS_COLORS[meeting.status] ?? "bg-gray-400";

  if (compact) {
    return (
      <button
        onClick={() => navigate(`/meetings/${meeting.id}`)}
        className="flex items-center gap-1.5 w-full text-left px-1 py-0.5 rounded text-[11px] leading-tight hover:bg-surface-hover transition-colors truncate"
        title={title}
      >
        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusColor}`} />
        <span className="truncate text-text-secondary">{title}</span>
      </button>
    );
  }

  let attendees: { name: string; email: string }[] = [];
  try {
    attendees = meeting.attendees_json
      ? JSON.parse(meeting.attendees_json)
      : [];
  } catch {
    // Malformed JSON — safe to ignore.
  }

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => navigate(`/meetings/${meeting.id}`)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          navigate(`/meetings/${meeting.id}`);
        }
      }}
      className="flex items-start gap-3 w-full text-left p-3 rounded-lg border border-border bg-surface-raised hover:bg-surface-hover transition-colors cursor-pointer"
    >
      <span className={`w-2 h-2 rounded-full mt-1.5 shrink-0 ${statusColor}`} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-text-primary truncate">
          {title}
        </p>
        <div className="flex items-center gap-2 mt-0.5 text-xs text-text-muted">
          {durationMin !== null && <span>{durationMin}m</span>}
          {attendees.length > 0 && (
            <span>
              {attendees.length} attendee{attendees.length > 1 ? "s" : ""}
            </span>
          )}
          {meeting.teams_join_url && (
            <span className="px-1 py-0.5 rounded bg-blue-500/10 text-blue-400 text-[10px] font-medium">
              Teams
            </span>
          )}
          {meeting.status !== "complete" && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                navigate(`/prep/${meeting.id}`);
              }}
              className="px-1.5 py-0.5 rounded bg-accent/10 text-accent text-[10px] font-medium hover:bg-accent/20 transition-colors"
            >
              Prep
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
