import { subDays, format, eachDayOfInterval, isToday } from "date-fns";
import { useState } from "react";
import type { Meeting } from "../../lib/types";

interface MeetingHeatmapProps {
  meetings: Meeting[];
}

const DAYS = 84; // 12 weeks

function getIntensity(count: number): string {
  if (count === 0) return "bg-surface-raised";
  if (count === 1) return "bg-accent/25";
  if (count <= 3) return "bg-accent/50";
  return "bg-accent/80";
}

export function MeetingHeatmap({ meetings }: MeetingHeatmapProps) {
  const [tooltip, setTooltip] = useState<{
    date: string;
    count: number;
    x: number;
    y: number;
  } | null>(null);
  const today = new Date();
  const start = subDays(today, DAYS - 1);
  const days = eachDayOfInterval({ start, end: today });

  // Count meetings per day
  const counts = new Map<string, number>();
  for (const meeting of meetings) {
    const key = format(new Date(meeting.started_at * 1000), "yyyy-MM-dd");
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }

  const totalMeetings = meetings.length;

  return (
    <div className="relative">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] text-text-muted">Last 12 weeks</span>
        <span className="text-[10px] text-text-secondary font-medium">
          {totalMeetings} meeting{totalMeetings !== 1 ? "s" : ""}
        </span>
      </div>
      <div
        className="flex flex-wrap gap-[2px]"
        onMouseLeave={() => setTooltip(null)}
      >
        {days.map((day) => {
          const key = format(day, "yyyy-MM-dd");
          const count = counts.get(key) ?? 0;
          const todayClass = isToday(day) ? "ring-1 ring-accent" : "";

          return (
            <div
              key={key}
              className={`w-[10px] h-[10px] rounded-sm ${getIntensity(count)} ${todayClass}`}
              onMouseEnter={(e) => {
                const rect = e.currentTarget.getBoundingClientRect();
                setTooltip({
                  date: format(day, "MMM d"),
                  count,
                  x: rect.left + rect.width / 2,
                  y: rect.top - 4,
                });
              }}
            />
          );
        })}
      </div>
      {tooltip && (
        <div
          className="fixed z-50 px-2 py-1 rounded bg-surface-raised border border-border shadow-sm text-[10px] text-text-secondary -translate-x-1/2 -translate-y-full pointer-events-none"
          style={{ left: tooltip.x, top: tooltip.y }}
        >
          {tooltip.date}: {tooltip.count} meeting
          {tooltip.count !== 1 ? "s" : ""}
        </div>
      )}
    </div>
  );
}
