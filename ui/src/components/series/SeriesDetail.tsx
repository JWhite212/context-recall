import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { format } from "date-fns";
import { getSeriesDetail, getSeriesTrends } from "../../lib/api";

export function SeriesDetail() {
  const { id } = useParams<{ id: string }>();

  const { data: series, isLoading: seriesLoading } = useQuery({
    queryKey: ["series", id],
    queryFn: () => getSeriesDetail(id!),
    enabled: !!id,
  });

  const { data: trends, isLoading: trendsLoading } = useQuery({
    queryKey: ["series-trends", id],
    queryFn: () => getSeriesTrends(id!),
    enabled: !!id,
  });

  const isLoading = seriesLoading || trendsLoading;

  // Loading skeleton
  if (isLoading) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <div className="h-7 w-64 bg-surface border border-border rounded animate-pulse mb-2" />
        <div className="h-4 w-48 bg-surface border border-border rounded animate-pulse mb-8" />
        <div className="h-32 bg-surface border border-border rounded animate-pulse mb-6" />
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-12 bg-surface border border-border rounded animate-pulse"
            />
          ))}
        </div>
      </div>
    );
  }

  // Empty state
  if (!series) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <p className="text-sm text-text-muted text-center py-16">
          Series not found.
        </p>
      </div>
    );
  }

  const meetings = series.meetings ?? [];
  const durationTrend = trends?.duration_trend ?? [];
  const maxDuration = Math.max(...durationTrend, 1);

  return (
    <div className="p-6 max-w-3xl mx-auto">
      {/* Header */}
      <h1 className="text-2xl font-bold text-text-primary">{series.title}</h1>
      <p className="text-xs text-text-muted mt-1">
        {series.detection_method}
        {series.typical_time && ` \u00B7 ${series.typical_time}`}
        {trends && ` \u00B7 ${trends.meeting_count} meetings`}
      </p>

      {/* Duration trend chart */}
      {durationTrend.length > 0 && (
        <div className="mt-6 rounded-xl bg-surface-raised border border-border p-4">
          <h2 className="text-sm font-medium text-text-primary mb-3">
            Duration Trend
          </h2>
          <div className="flex items-end gap-1 h-24">
            {durationTrend.map((value, i) => (
              <div
                key={i}
                className="flex-1 bg-accent/70 rounded-t transition-all"
                style={{ height: `${(value / maxDuration) * 100}%` }}
                title={`${value} min`}
              />
            ))}
          </div>
          {trends && trends.avg_duration_minutes != null && (
            <p className="text-xs text-text-muted mt-2">
              Avg: {trends.avg_duration_minutes.toFixed(0)} min
            </p>
          )}
        </div>
      )}

      {/* Meetings list */}
      <div className="mt-6">
        <h2 className="text-sm font-medium text-text-primary mb-3">Meetings</h2>
        {meetings.length === 0 ? (
          <p className="text-sm text-text-muted text-center py-8">
            No meetings linked to this series yet.
          </p>
        ) : (
          <div className="flex flex-col gap-1">
            {meetings.map((m) => (
              <Link
                key={m.id}
                to={`/meetings/${m.id}`}
                className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-sidebar-hover transition-colors"
              >
                <div className="min-w-0">
                  <p className="text-sm text-text-primary truncate">
                    {m.title}
                  </p>
                  <p className="text-xs text-text-muted">
                    {m.started_at != null
                      ? format(m.started_at * 1000, "MMM d, yyyy")
                      : "Date unknown"}
                  </p>
                </div>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
