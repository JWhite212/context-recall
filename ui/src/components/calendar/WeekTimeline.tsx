import {
  startOfWeek,
  endOfWeek,
  eachDayOfInterval,
  format,
  isSameDay,
  isToday,
  getHours,
  getMinutes,
} from "date-fns";
import { useNavigate } from "react-router-dom";
import type { CalendarEvent, Meeting } from "../../lib/types";

interface WeekTimelineProps {
  currentDate: Date;
  meetings: Meeting[];
  events?: CalendarEvent[];
}

const START_HOUR = 7;
const END_HOUR = 22;
const TOTAL_HOURS = END_HOUR - START_HOUR;
const HOUR_HEIGHT = 48; // px per hour

export function WeekTimeline({
  currentDate,
  meetings,
  events = [],
}: WeekTimelineProps) {
  const navigate = useNavigate();
  const weekStart = startOfWeek(currentDate, { weekStartsOn: 1 });
  const weekEnd = endOfWeek(currentDate, { weekStartsOn: 1 });
  const days = eachDayOfInterval({ start: weekStart, end: weekEnd });

  const hours = Array.from({ length: TOTAL_HOURS }, (_, i) => START_HOUR + i);

  return (
    <div className="flex flex-1 overflow-auto">
      {/* Time gutter */}
      <div className="w-12 shrink-0 border-r border-border">
        <div className="h-8" /> {/* Header spacer */}
        {hours.map((hour) => (
          <div
            key={hour}
            className="h-[48px] flex items-start justify-end pr-2 text-[10px] text-text-muted -mt-[5px]"
          >
            {format(new Date(2000, 0, 1, hour), "HH:mm")}
          </div>
        ))}
      </div>

      {/* Day columns */}
      <div className="flex flex-1">
        {days.map((day) => {
          const dayKey = format(day, "yyyy-MM-dd");
          const today = isToday(day);
          const dayMeetings = meetings.filter((m) =>
            isSameDay(new Date(m.started_at * 1000), day),
          );
          const dayEvents = events.filter((ev) =>
            isSameDay(new Date(ev.start_ts * 1000), day),
          );

          return (
            <div
              key={dayKey}
              className={`flex-1 border-r border-border min-w-[100px] ${
                today ? "bg-accent/5" : ""
              }`}
            >
              {/* Day header */}
              <div className="h-8 flex items-center justify-center border-b border-border">
                <span
                  className={`text-[11px] font-medium px-1.5 py-0.5 rounded ${
                    today ? "bg-accent text-white" : "text-text-secondary"
                  }`}
                >
                  {format(day, "EEE d")}
                </span>
              </div>

              {/* Time slots */}
              <div
                className="relative"
                style={{ height: `${TOTAL_HOURS * HOUR_HEIGHT}px` }}
              >
                {/* Hour lines */}
                {hours.map((hour) => (
                  <div
                    key={hour}
                    className="absolute w-full border-t border-border/50"
                    style={{ top: `${(hour - START_HOUR) * HOUR_HEIGHT}px` }}
                  />
                ))}

                {/* Meeting blocks */}
                {dayMeetings.map((meeting) => {
                  const meetingDate = new Date(meeting.started_at * 1000);
                  const startHour =
                    getHours(meetingDate) + getMinutes(meetingDate) / 60;
                  const duration = meeting.duration_seconds
                    ? meeting.duration_seconds / 3600
                    : 0.5; // default 30min
                  // Clamp position within the visible range
                  const clampedStart = Math.max(
                    START_HOUR,
                    Math.min(startHour, END_HOUR),
                  );
                  const clampedEnd = Math.min(END_HOUR, startHour + duration);
                  const top = (clampedStart - START_HOUR) * HOUR_HEIGHT;
                  const height = Math.max(
                    20,
                    (clampedEnd - clampedStart) * HOUR_HEIGHT,
                  );
                  const isOutOfRange =
                    startHour < START_HOUR || startHour >= END_HOUR;

                  return (
                    <button
                      key={meeting.id}
                      onClick={() => navigate(`/meetings/${meeting.id}`)}
                      className={`absolute left-0.5 right-0.5 rounded px-1.5 py-0.5 border hover:bg-accent/30 transition-colors overflow-hidden cursor-pointer ${
                        isOutOfRange
                          ? "bg-amber-500/10 border-dashed border-amber-500/30"
                          : "bg-accent/20 border-accent/30"
                      }`}
                      style={{ top: `${top}px`, height: `${height}px` }}
                      title={`${meeting.title || "Untitled"}${isOutOfRange ? " (outside visible hours)" : ""}`}
                    >
                      <p className="text-[10px] font-medium text-text-primary truncate">
                        {meeting.title || "Untitled"}
                      </p>
                      {height > 30 && (
                        <p className="text-[9px] text-text-muted">
                          {format(meetingDate, "HH:mm")}
                          {meeting.duration_seconds && (
                            <>
                              {" "}
                              &middot;{" "}
                              {Math.round(meeting.duration_seconds / 60)}m
                            </>
                          )}
                        </p>
                      )}
                    </button>
                  );
                })}

                {/* Upcoming event blocks */}
                {dayEvents.map((ev) => {
                  const eventDate = new Date(ev.start_ts * 1000);
                  const startHour =
                    getHours(eventDate) + getMinutes(eventDate) / 60;
                  const duration = (ev.end_ts - ev.start_ts) / 3600;
                  const clampedStart = Math.max(
                    START_HOUR,
                    Math.min(startHour, END_HOUR),
                  );
                  const clampedEnd = Math.min(END_HOUR, startHour + duration);
                  const top = (clampedStart - START_HOUR) * HOUR_HEIGHT;
                  const height = Math.max(
                    20,
                    (clampedEnd - clampedStart) * HOUR_HEIGHT,
                  );

                  return (
                    <div
                      key={ev.event_uid}
                      className="absolute left-0.5 right-0.5 rounded px-1.5 py-0.5 border border-dashed border-text-muted/40 bg-surface-hover/40 overflow-hidden"
                      style={{ top: `${top}px`, height: `${height}px` }}
                      title={ev.title || "Untitled"}
                    >
                      <p className="text-[10px] font-medium text-text-secondary truncate">
                        {ev.title || "Untitled"}
                      </p>
                      {height > 30 && (
                        <p className="text-[9px] text-text-muted">
                          {format(eventDate, "HH:mm")}
                        </p>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
