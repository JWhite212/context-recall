import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { getSeries } from "../../lib/api";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import type { MeetingSeries } from "../../lib/types";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const METHOD_BADGE: Record<
  MeetingSeries["detection_method"],
  { bg: string; text: string }
> = {
  calendar: { bg: "bg-blue-400/20", text: "text-blue-400" },
  heuristic: { bg: "bg-amber-400/20", text: "text-amber-400" },
  manual: { bg: "bg-green-400/20", text: "text-green-400" },
};

function formatSchedule(series: MeetingSeries): string {
  if (series.typical_day_of_week == null && series.typical_time == null) {
    return "No schedule";
  }
  const day =
    series.typical_day_of_week != null
      ? DAYS[series.typical_day_of_week]
      : null;
  const time = series.typical_time ?? null;
  return [day, time].filter(Boolean).join(" ");
}

export function SeriesList() {
  const navigate = useNavigate();

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["series"],
    queryFn: getSeries,
  });

  const seriesList = data?.series ?? [];

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-semibold text-text-primary">Series</h1>
        {!isLoading && !isError && (
          <span className="text-xs text-text-muted">({seriesList.length})</span>
        )}
      </div>

      {isLoading ? (
        <div className="rounded-xl bg-surface-raised border border-border p-6">
          <div className="flex flex-col gap-2">
            {Array.from({ length: 3 }).map((_, i) => (
              <SkeletonCard key={i} />
            ))}
          </div>
        </div>
      ) : isError ? (
        <ErrorState
          message="Failed to load series."
          onRetry={() => refetch()}
        />
      ) : seriesList.length === 0 ? (
        <EmptyState
          title="No series yet"
          description="Meeting series will appear here once recurring meetings are detected."
          icon={
            <svg
              width="36"
              height="36"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-text-muted/50"
            >
              <rect x="3" y="4" width="18" height="18" rx="2" ry="2" />
              <line x1="16" y1="2" x2="16" y2="6" />
              <line x1="8" y1="2" x2="8" y2="6" />
              <line x1="3" y1="10" x2="21" y2="10" />
            </svg>
          }
        />
      ) : (
        <div className="rounded-xl bg-surface-raised border border-border p-6">
          <div className="flex flex-col gap-1">
            {seriesList.map((s) => {
              const badge = METHOD_BADGE[s.detection_method];
              return (
                <button
                  key={s.id}
                  onClick={() => navigate(`/series/${s.id}`)}
                  className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-sidebar-hover transition-colors cursor-pointer text-left"
                >
                  <div className="min-w-0 flex items-center gap-2">
                    <p className="text-sm font-medium text-text-primary truncate">
                      {s.title}
                    </p>
                    <span
                      className={`text-xs px-2 py-0.5 rounded-full ${badge.bg} ${badge.text}`}
                    >
                      {s.detection_method}
                    </span>
                  </div>
                  <span className="text-xs text-text-muted whitespace-nowrap ml-3">
                    {formatSchedule(s)}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
