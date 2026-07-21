import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { format } from "date-fns";
import type { Meeting } from "../../lib/types";
import { getCalendarEvents, linkMeetingToCalendarEvent } from "../../lib/api";
import { CalendarLinkPicker, type LinkCandidate } from "./CalendarLinkPicker";

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

  // Link-to-calendar-event affordance — hooks must run unconditionally, so
  // these live above the compact-mode early return even though the menu
  // only renders in full mode.
  const qc = useQueryClient();
  const [menuOpen, setMenuOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const linked = !!meeting.calendar_event_uid;

  // Nearby unlinked calendar entries (± a day) for the picker.
  const anchor = meeting.started_at;
  const eventsQuery = useQuery({
    queryKey: ["calendar-events", "picker", meeting.id],
    queryFn: () => getCalendarEvents(anchor - 86400, anchor + 86400),
    enabled: pickerOpen,
    staleTime: 30_000,
  });
  const candidates: LinkCandidate[] = (eventsQuery.data?.events ?? []).map(
    (e) => ({
      id: e.event_uid,
      label: e.title || "Untitled",
      subtitle: format(new Date(e.start_ts * 1000), "EEE HH:mm"),
    }),
  );

  const link = useMutation({
    mutationFn: (eventUid: string) => {
      const ev = (eventsQuery.data?.events ?? []).find(
        (e) => e.event_uid === eventUid,
      )!;
      return linkMeetingToCalendarEvent(meeting.id, ev);
    },
    onSuccess: () => {
      setPickerOpen(false);
      setMenuOpen(false);
      void qc.invalidateQueries({ queryKey: ["calendar"] });
      void qc.invalidateQueries({ queryKey: ["calendar-events"] });
      void qc.invalidateQueries({ queryKey: ["meeting", meeting.id] });
    },
  });

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
        {linked && meeting.calendar_event_title && (
          <p className="mt-0.5 text-[11px] text-text-muted truncate">
            ↳ linked to {meeting.calendar_event_title}
          </p>
        )}
      </div>
      <div className="relative shrink-0" onClick={(e) => e.stopPropagation()}>
        <button
          type="button"
          aria-label="link options"
          onClick={() => setMenuOpen((v) => !v)}
          className="px-1 text-text-muted hover:text-text-secondary"
        >
          ⋯
        </button>
        {menuOpen && !pickerOpen && (
          <div className="absolute right-0 z-10 mt-1 w-48 rounded-lg border border-border bg-surface-raised p-1 shadow-lg text-xs">
            {linked ? (
              <span className="block px-2 py-1 text-text-muted">
                Linked to a calendar event
              </span>
            ) : (
              <button
                type="button"
                onClick={() => setPickerOpen(true)}
                className="w-full text-left px-2 py-1 rounded hover:bg-surface-hover text-text-primary"
              >
                Link to calendar event
              </button>
            )}
          </div>
        )}
        {pickerOpen && (
          <CalendarLinkPicker
            title="Link to calendar event"
            candidates={candidates}
            emptyLabel="No nearby calendar entries"
            busy={link.isPending}
            onPick={(id) => link.mutate(id)}
            onClose={() => setPickerOpen(false)}
          />
        )}
      </div>
    </div>
  );
}
