import { format, isSameDay } from "date-fns";
import type { CalendarEvent, Meeting } from "../../lib/types";
import { EventCard } from "./EventCard";
import { UpcomingEventCard } from "./UpcomingEventCard";

interface DayDetailProps {
  currentDate: Date;
  meetings: Meeting[];
  events?: CalendarEvent[];
  preparedUids?: Set<string>;
}

export function DayDetail({
  currentDate,
  meetings,
  events = [],
  preparedUids,
}: DayDetailProps) {
  const dayMeetings = meetings
    .filter((m) => isSameDay(new Date(m.started_at * 1000), currentDate))
    .sort((a, b) => a.started_at - b.started_at);

  const dayEvents = events
    .filter((ev) => isSameDay(new Date(ev.start_ts * 1000), currentDate))
    .sort((a, b) => a.start_ts - b.start_ts);

  return (
    <div className="flex flex-col flex-1 p-4 overflow-auto">
      <h2 className="text-sm font-medium text-text-primary mb-4">
        {format(currentDate, "EEEE, MMMM d, yyyy")}
      </h2>

      {dayMeetings.length === 0 && dayEvents.length === 0 ? (
        <div className="flex-1 flex items-center justify-center">
          <p className="text-sm text-text-muted">
            Nothing scheduled on this day
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {dayMeetings.map((meeting) => {
            const startTime = format(
              new Date(meeting.started_at * 1000),
              "HH:mm",
            );
            const endTime = meeting.ended_at
              ? format(new Date(meeting.ended_at * 1000), "HH:mm")
              : null;

            return (
              <div key={meeting.id} className="flex items-start gap-3">
                <div className="w-16 shrink-0 pt-3 text-right">
                  <p className="text-xs font-medium text-text-secondary">
                    {startTime}
                  </p>
                  {endTime && (
                    <p className="text-[10px] text-text-muted">{endTime}</p>
                  )}
                </div>
                <div className="w-px bg-border self-stretch shrink-0" />
                <div className="flex-1">
                  <EventCard meeting={meeting} />
                </div>
              </div>
            );
          })}
          {dayEvents.map((ev) => (
            <div key={ev.event_uid} className="flex items-start gap-3">
              <div className="w-16 shrink-0 pt-1 text-right">
                <p className="text-xs font-medium text-text-secondary">
                  {format(new Date(ev.start_ts * 1000), "HH:mm")}
                </p>
                <p className="text-[10px] text-text-muted">
                  {format(new Date(ev.end_ts * 1000), "HH:mm")}
                </p>
              </div>
              <div className="w-px bg-border self-stretch shrink-0" />
              <div className="flex-1">
                <UpcomingEventCard event={ev} preparedUids={preparedUids} />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
