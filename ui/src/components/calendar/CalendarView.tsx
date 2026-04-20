import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  startOfMonth,
  endOfMonth,
  startOfWeek,
  endOfWeek,
  startOfDay,
  endOfDay,
  addMonths,
  subMonths,
  addWeeks,
  subWeeks,
  addDays,
  subDays,
  format,
  getUnixTime,
} from "date-fns";
import { getCalendarMeetings } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { MonthGrid } from "./MonthGrid";
import { WeekTimeline } from "./WeekTimeline";
import { DayDetail } from "./DayDetail";
import { AgendaList } from "./AgendaList";
import { MeetingHeatmap } from "./MeetingHeatmap";

type ViewMode = "month" | "week" | "day" | "agenda";

const VIEW_LABELS: { value: ViewMode; label: string }[] = [
  { value: "month", label: "Month" },
  { value: "week", label: "Week" },
  { value: "day", label: "Day" },
  { value: "agenda", label: "Agenda" },
];

function getDateRange(
  date: Date,
  mode: ViewMode,
): { start: number; end: number } {
  switch (mode) {
    case "month":
    case "agenda": {
      const ms = startOfWeek(startOfMonth(date), { weekStartsOn: 1 });
      const me = endOfWeek(endOfMonth(date), { weekStartsOn: 1 });
      return { start: getUnixTime(ms), end: getUnixTime(me) + 1 };
    }
    case "week": {
      const ws = startOfWeek(date, { weekStartsOn: 1 });
      const we = endOfWeek(date, { weekStartsOn: 1 });
      return { start: getUnixTime(ws), end: getUnixTime(we) + 1 };
    }
    case "day": {
      const ds = startOfDay(date);
      const de = endOfDay(date);
      return { start: getUnixTime(ds), end: getUnixTime(de) + 1 };
    }
  }
}

export function CalendarView() {
  const { daemonRunning } = useDaemonStatus();
  const [currentDate, setCurrentDate] = useState(new Date());
  const [viewMode, setViewMode] = useState<ViewMode>("month");

  const { start, end } = useMemo(
    () => getDateRange(currentDate, viewMode),
    [currentDate, viewMode],
  );

  // Query for the active view
  const { data } = useQuery({
    queryKey: ["calendar", start, end],
    queryFn: () => getCalendarMeetings(start, end),
    enabled: daemonRunning,
    staleTime: 30_000,
  });

  // Heatmap needs last 84 days regardless of view
  const heatmapRange = useMemo(() => {
    const today = new Date();
    const hStart = subDays(today, 83);
    return {
      start: getUnixTime(startOfDay(hStart)),
      end: getUnixTime(endOfDay(today)) + 1,
    };
  }, []);

  const { data: heatmapData } = useQuery({
    queryKey: ["calendar-heatmap", heatmapRange.start, heatmapRange.end],
    queryFn: () => getCalendarMeetings(heatmapRange.start, heatmapRange.end),
    enabled: daemonRunning,
    staleTime: 60_000,
  });

  const meetings = data?.meetings ?? [];
  const heatmapMeetings = heatmapData?.meetings ?? [];

  function navigate(direction: "prev" | "next" | "today") {
    if (direction === "today") {
      setCurrentDate(new Date());
      return;
    }
    const delta = direction === "next" ? 1 : -1;
    switch (viewMode) {
      case "month":
      case "agenda":
        setCurrentDate((d) => (delta > 0 ? addMonths(d, 1) : subMonths(d, 1)));
        break;
      case "week":
        setCurrentDate((d) => (delta > 0 ? addWeeks(d, 1) : subWeeks(d, 1)));
        break;
      case "day":
        setCurrentDate((d) => (delta > 0 ? addDays(d, 1) : subDays(d, 1)));
        break;
    }
  }

  function handleDayClick(date: Date) {
    setCurrentDate(date);
    setViewMode("day");
  }

  const headerLabel = (() => {
    switch (viewMode) {
      case "month":
      case "agenda":
        return format(currentDate, "MMMM yyyy");
      case "week":
        return `Week of ${format(startOfWeek(currentDate, { weekStartsOn: 1 }), "MMM d, yyyy")}`;
      case "day":
        return format(currentDate, "EEEE, MMMM d, yyyy");
    }
  })();

  return (
    <div className="flex flex-col h-full px-6 pb-4">
      {/* Heatmap */}
      <div className="mb-4">
        <MeetingHeatmap meetings={heatmapMeetings} />
      </div>

      {/* Header: navigation + view mode */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <button
            onClick={() => navigate("prev")}
            className="p-1.5 rounded hover:bg-surface-hover transition-colors text-text-secondary"
            aria-label="Previous"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <polyline points="15 18 9 12 15 6" />
            </svg>
          </button>
          <button
            onClick={() => navigate("today")}
            className="px-2 py-1 text-xs font-medium text-text-secondary hover:bg-surface-hover rounded transition-colors"
          >
            Today
          </button>
          <button
            onClick={() => navigate("next")}
            className="p-1.5 rounded hover:bg-surface-hover transition-colors text-text-secondary"
            aria-label="Next"
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
            >
              <polyline points="9 18 15 12 9 6" />
            </svg>
          </button>
          <h1 className="text-sm font-semibold text-text-primary ml-2">
            {headerLabel}
          </h1>
        </div>

        {/* View mode tabs */}
        <div className="flex items-center gap-0.5 p-0.5 bg-surface-raised rounded-md border border-border">
          {VIEW_LABELS.map((v) => (
            <button
              key={v.value}
              onClick={() => setViewMode(v.value)}
              className={`px-2.5 py-1 text-xs font-medium rounded transition-colors ${
                viewMode === v.value
                  ? "bg-surface text-text-primary shadow-sm"
                  : "text-text-muted hover:text-text-secondary"
              }`}
            >
              {v.label}
            </button>
          ))}
        </div>
      </div>

      {/* Active view */}
      <div className="flex-1 border border-border rounded-lg overflow-hidden flex flex-col bg-surface">
        {viewMode === "month" && (
          <MonthGrid
            currentDate={currentDate}
            meetings={meetings}
            onDayClick={handleDayClick}
          />
        )}
        {viewMode === "week" && (
          <WeekTimeline currentDate={currentDate} meetings={meetings} />
        )}
        {viewMode === "day" && (
          <DayDetail currentDate={currentDate} meetings={meetings} />
        )}
        {viewMode === "agenda" && <AgendaList meetings={meetings} />}
      </div>
    </div>
  );
}
