import { useState } from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { CalendarEvent } from "../../lib/types";
import {
  generatePrepForEvent,
  getCalendarMeetings,
  linkMeetingToCalendarEvent,
  startRecording,
} from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useToast } from "../common/Toast";
import { PrepModal } from "./PrepModal";
import { CalendarLinkPicker, type LinkCandidate } from "./CalendarLinkPicker";

interface UpcomingEventCardProps {
  event: CalendarEvent;
  compact?: boolean;
  preparedUids?: Set<string>;
}

/** Renders an imported (not-yet-recorded) calendar event, with interactive prep/record actions. */
export function UpcomingEventCard({
  event,
  compact = false,
  preparedUids,
}: UpcomingEventCardProps) {
  const [open, setOpen] = useState(false);
  const [showPrep, setShowPrep] = useState(false);
  const [confirmingRecord, setConfirmingRecord] = useState(false);
  const queryClient = useQueryClient();
  const { state } = useDaemonStatus();
  const toast = useToast();

  const title = event.title || "Untitled";
  const start = format(new Date(event.start_ts * 1000), "HH:mm");
  const prepared = preparedUids?.has(event.event_uid) ?? false;

  const isRecording = state === "recording";
  const nowSec = Date.now() / 1000;
  const live = event.start_ts - 300 <= nowSec && nowSec <= event.end_ts;

  const generate = useMutation({
    mutationFn: () =>
      generatePrepForEvent({
        event_uid: event.event_uid,
        title,
        attendees: event.attendees,
        attendee_names: event.attendees.map((a) => a.name || a.email),
        end_ts: event.end_ts,
        series_id: null,
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(["prep", "by-event", event.event_uid], data);
      void queryClient.invalidateQueries({ queryKey: ["prepared-events"] });
      setShowPrep(true);
    },
    onError: () => toast.error("Failed to generate prep."),
  });

  const record = useMutation({
    mutationFn: () => startRecording(),
    onSuccess: () => setConfirmingRecord(false),
    onError: () => toast.error("Failed to start recording."),
  });

  const [assigning, setAssigning] = useState(false);
  const meetingsQuery = useQuery({
    queryKey: ["calendar", "assign-picker", event.event_uid],
    queryFn: () =>
      getCalendarMeetings(event.start_ts - 86400, event.end_ts + 86400),
    enabled: assigning,
    staleTime: 30_000,
  });
  const recCandidates: LinkCandidate[] = (meetingsQuery.data?.meetings ?? [])
    .filter((m) => !m.calendar_event_uid)
    .map((m) => ({
      id: m.id,
      label: m.title || "Untitled",
      subtitle: format(new Date(m.started_at * 1000), "EEE HH:mm"),
    }));
  const assign = useMutation({
    mutationFn: (meetingId: string) =>
      linkMeetingToCalendarEvent(meetingId, event),
    onSuccess: () => {
      setAssigning(false);
      setOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["calendar"] });
      void queryClient.invalidateQueries({ queryKey: ["calendar-events"] });
    },
    onError: () => toast.error("Failed to link the recording."),
  });

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={title}
        className={`w-full text-left rounded border border-dashed border-text-muted/40 bg-surface-hover/40 text-text-secondary hover:border-accent/50 transition-colors ${
          compact ? "px-1 py-0.5 text-[10px]" : "px-2 py-1 text-xs"
        }`}
      >
        <span className="truncate block">
          {!compact && <span className="text-text-muted mr-1">{start}</span>}
          {title}
          {prepared && (
            <span className="ml-1 rounded bg-accent/20 text-accent px-1 text-[9px] align-middle">
              Prep ready
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="absolute z-10 mt-1 w-56 rounded-lg border border-border bg-surface-raised p-3 shadow-lg text-xs">
          <p className="font-medium text-text-primary">{title}</p>
          <p className="text-text-muted mt-0.5">
            {format(new Date(event.start_ts * 1000), "EEE d MMM, HH:mm")} –{" "}
            {format(new Date(event.end_ts * 1000), "HH:mm")}
          </p>
          {event.attendees.length > 0 && (
            <ul className="mt-2 flex flex-col gap-0.5">
              {event.attendees.map((a, i) => (
                <li
                  key={`${a.email || a.name}-${i}`}
                  className="text-text-secondary"
                >
                  {a.name || a.email}
                </li>
              ))}
            </ul>
          )}
          {event.join_url && (
            <a
              href={event.join_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-accent hover:underline"
            >
              Join
            </a>
          )}

          <div className="mt-3 flex flex-col gap-1.5 border-t border-border pt-2">
            {prepared && (
              <button
                type="button"
                onClick={() => setShowPrep(true)}
                className="text-left text-accent hover:underline"
              >
                View prep
              </button>
            )}
            <button
              type="button"
              onClick={() => generate.mutate()}
              disabled={generate.isPending}
              className="text-left text-accent hover:underline disabled:opacity-50"
            >
              {generate.isPending
                ? "Generating..."
                : prepared
                  ? "Regenerate prep"
                  : "Generate prep"}
            </button>

            {!confirmingRecord ? (
              <button
                type="button"
                onClick={() => setConfirmingRecord(true)}
                disabled={!live || isRecording}
                title={
                  isRecording
                    ? "Already recording"
                    : live
                      ? ""
                      : "Available when the meeting is live"
                }
                className="text-left text-accent hover:underline disabled:opacity-40 disabled:no-underline disabled:text-text-muted"
              >
                Record this meeting
              </button>
            ) : (
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => record.mutate()}
                  disabled={record.isPending}
                  className="text-left text-accent hover:underline disabled:opacity-50"
                >
                  Start recording?
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingRecord(false)}
                  className="text-text-muted hover:text-text-secondary"
                >
                  Cancel
                </button>
              </div>
            )}

            <button
              type="button"
              onClick={() => setAssigning(true)}
              className="text-left text-accent hover:underline"
            >
              Assign a recording
            </button>
          </div>

          {assigning && (
            <CalendarLinkPicker
              title="Assign a recording"
              candidates={recCandidates}
              emptyLabel="No nearby recordings"
              busy={assign.isPending}
              onPick={(id) => assign.mutate(id)}
              onClose={() => setAssigning(false)}
            />
          )}
        </div>
      )}

      {showPrep && (
        <PrepModal
          eventUid={event.event_uid}
          title={title}
          onClose={() => setShowPrep(false)}
        />
      )}
    </div>
  );
}
