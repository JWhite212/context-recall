import { useState } from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Meeting } from "../../lib/types";
import {
  getCalendarEvents,
  getCalendarMeetings,
  linkMeetingToCalendarEvent,
  unlinkMeetingFromCalendarEvent,
} from "../../lib/api";
import {
  CalendarLinkPicker,
  type LinkCandidate,
} from "../calendar/CalendarLinkPicker";
import { useToast } from "../common/Toast";

export function CalendarLinkCard({ meeting }: { meeting: Meeting }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [pickerOpen, setPickerOpen] = useState(false);
  const linked = !!meeting.calendar_event_uid;

  const eventsQuery = useQuery({
    queryKey: ["calendar-events", "picker", meeting.id],
    queryFn: () =>
      getCalendarEvents(meeting.started_at - 86400, meeting.started_at + 86400),
    enabled: pickerOpen,
    staleTime: 30_000,
  });
  // Nearby recordings, to exclude calendar entries already claimed by
  // another recording (linking to one would just 409).
  const meetingsForPicker = useQuery({
    queryKey: ["calendar", "detail-link-picker", meeting.id],
    queryFn: () =>
      getCalendarMeetings(
        meeting.started_at - 86400,
        meeting.started_at + 86400,
      ),
    enabled: pickerOpen,
    staleTime: 30_000,
  });
  const linkedUids = new Set(
    (meetingsForPicker.data?.meetings ?? [])
      .map((m) => m.calendar_event_uid)
      .filter((u): u is string => !!u),
  );
  const candidates: LinkCandidate[] = (eventsQuery.data?.events ?? [])
    .filter((e) => !linkedUids.has(e.event_uid))
    .map((e) => ({
      id: e.event_uid,
      label: e.title || "Untitled",
      subtitle: format(new Date(e.start_ts * 1000), "EEE HH:mm"),
    }));

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["meeting", meeting.id] });
    void qc.invalidateQueries({ queryKey: ["meetings"] });
    void qc.invalidateQueries({ queryKey: ["calendar"] });
    void qc.invalidateQueries({ queryKey: ["calendar-events"] });
  };

  const link = useMutation({
    mutationFn: (eventUid: string) => {
      const ev = (eventsQuery.data?.events ?? []).find(
        (e) => e.event_uid === eventUid,
      )!;
      return linkMeetingToCalendarEvent(meeting.id, ev);
    },
    onSuccess: () => {
      setPickerOpen(false);
      invalidate();
    },
    onError: () => toast.error("Failed to link."),
  });
  const unlink = useMutation({
    mutationFn: () => unlinkMeetingFromCalendarEvent(meeting.id),
    onSuccess: invalidate,
    onError: () => toast.error("Failed to unlink."),
  });

  return (
    <div className="relative mt-3 rounded-lg bg-surface-raised border border-border p-3 text-xs">
      {linked ? (
        <div className="flex items-center justify-between gap-2">
          <span className="text-text-primary truncate">
            Linked to {meeting.calendar_event_title || "a calendar event"}
          </span>
          <button
            type="button"
            onClick={() => unlink.mutate()}
            disabled={unlink.isPending}
            className="text-text-muted hover:text-text-secondary shrink-0"
          >
            Unlink
          </button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setPickerOpen(true)}
          className="text-accent hover:underline"
        >
          Link to calendar event
        </button>
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
  );
}
