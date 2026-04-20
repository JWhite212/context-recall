import {
  startOfMonth,
  endOfMonth,
  startOfWeek,
  endOfWeek,
  eachDayOfInterval,
  isSameMonth,
  isToday,
  format,
} from "date-fns";
import type { Meeting } from "../../lib/types";
import { EventCard } from "./EventCard";

interface MonthGridProps {
  currentDate: Date;
  meetings: Meeting[];
  onDayClick: (date: Date) => void;
}

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function MonthGrid({
  currentDate,
  meetings,
  onDayClick,
}: MonthGridProps) {
  const monthStart = startOfMonth(currentDate);
  const monthEnd = endOfMonth(currentDate);
  const gridStart = startOfWeek(monthStart, { weekStartsOn: 1 });
  const gridEnd = endOfWeek(monthEnd, { weekStartsOn: 1 });
  const days = eachDayOfInterval({ start: gridStart, end: gridEnd });

  const meetingsByDay = new Map<string, Meeting[]>();
  for (const meeting of meetings) {
    const day = format(new Date(meeting.started_at * 1000), "yyyy-MM-dd");
    const list = meetingsByDay.get(day) ?? [];
    list.push(meeting);
    meetingsByDay.set(day, list);
  }

  return (
    <div className="flex flex-col flex-1">
      {/* Weekday headers */}
      <div className="grid grid-cols-7 border-b border-border">
        {WEEKDAYS.map((day) => (
          <div
            key={day}
            className="px-2 py-1.5 text-[11px] font-medium text-text-muted text-center"
          >
            {day}
          </div>
        ))}
      </div>

      {/* Day cells */}
      <div className="grid grid-cols-7 flex-1">
        {days.map((day) => {
          const key = format(day, "yyyy-MM-dd");
          const dayMeetings = meetingsByDay.get(key) ?? [];
          const inMonth = isSameMonth(day, currentDate);
          const today = isToday(day);

          return (
            <button
              key={key}
              onClick={() => onDayClick(day)}
              className={`flex flex-col border-b border-r border-border p-1 min-h-[80px] text-left transition-colors hover:bg-surface-hover ${
                !inMonth ? "opacity-40" : ""
              }`}
            >
              <span
                className={`text-[11px] font-medium w-5 h-5 flex items-center justify-center rounded-full ${
                  today ? "bg-accent text-white" : "text-text-secondary"
                }`}
              >
                {format(day, "d")}
              </span>
              <div className="flex flex-col gap-0.5 mt-0.5 overflow-hidden flex-1">
                {dayMeetings.slice(0, 3).map((m) => (
                  <EventCard key={m.id} meeting={m} compact />
                ))}
                {dayMeetings.length > 3 && (
                  <span className="text-[10px] text-text-muted px-1">
                    +{dayMeetings.length - 3} more
                  </span>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}
