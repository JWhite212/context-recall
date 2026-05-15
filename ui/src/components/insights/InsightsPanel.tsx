import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  getAnalyticsSummary,
  getAnalyticsTrends,
  getAnalyticsPeople,
  getAnalyticsHealth,
} from "../../lib/api";
import { StatCard } from "./StatCard";
import { TrendChart } from "./TrendChart";
import { PeopleRanking } from "./PeopleRanking";
import { HealthAlerts } from "./HealthAlerts";
import { Skeleton, SkeletonLine } from "../common/Skeleton";

type Period = "daily" | "weekly" | "monthly";

const PERIOD_OPTIONS: { value: Period; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

export function InsightsPanel() {
  const [period, setPeriod] = useState<Period>("weekly");

  const { data: summary, isLoading: summaryLoading } = useQuery({
    queryKey: ["analytics-summary", period],
    queryFn: () => getAnalyticsSummary(period),
  });
  const { data: trends, isLoading: trendsLoading } = useQuery({
    queryKey: ["analytics-trends", period],
    queryFn: () => getAnalyticsTrends(period, 8),
  });
  const { data: people, isLoading: peopleLoading } = useQuery({
    queryKey: ["analytics-people"],
    queryFn: () => getAnalyticsPeople(10),
  });
  const { data: health, isLoading: healthLoading } = useQuery({
    queryKey: ["analytics-health"],
    queryFn: getAnalyticsHealth,
  });

  const current = summary?.current_period;

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-lg font-semibold text-text-primary">Insights</h1>
        <div
          className="flex gap-1 bg-surface border border-border rounded-lg p-0.5"
          role="group"
          aria-label="Period selector"
        >
          {PERIOD_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setPeriod(opt.value)}
              aria-pressed={period === opt.value}
              className={`px-3 py-1 text-xs rounded-md transition-colors ${
                period === opt.value
                  ? "bg-accent text-white"
                  : "text-text-muted hover:text-text-primary"
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* Stat grid */}
      {summaryLoading || healthLoading ? (
        <div
          className="grid grid-cols-4 gap-3 mb-6"
          role="status"
          aria-label="Loading insights summary"
        >
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="p-4 bg-surface-raised border border-border rounded-lg"
              aria-hidden="true"
            >
              <SkeletonLine width="w-16" />
              <Skeleton className="h-6 w-20 mt-2" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-4 gap-3 mb-6">
          <StatCard label="Meetings" value={current?.total_meetings ?? 0} />
          <StatCard
            label="Hours"
            value={
              current ? Math.round(current.total_duration_minutes / 60) : 0
            }
          />
          <StatCard label="Load" value={health?.load_score.label ?? "N/A"} />
          <StatCard label="Attendees" value={current?.unique_attendees ?? 0} />
        </div>
      )}

      {/* Trend + People row */}
      {trendsLoading || peopleLoading ? (
        <div
          className="grid grid-cols-2 gap-3 mb-6"
          role="status"
          aria-label="Loading trend and people data"
        >
          {Array.from({ length: 2 }).map((_, i) => (
            <div
              key={i}
              className="p-4 bg-surface-raised border border-border rounded-lg"
              aria-hidden="true"
            >
              <SkeletonLine width="w-32" />
              <Skeleton className="h-24 w-full mt-3" />
            </div>
          ))}
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3 mb-6">
          {trends && trends.trends.length > 0 ? (
            <TrendChart
              periods={trends.trends}
              metricKey="total_meetings"
              label="Meetings per Week"
            />
          ) : (
            <div className="p-4 bg-surface-raised border border-border rounded-lg">
              <p className="text-xs text-text-muted">Meetings per Week</p>
              <p className="text-sm text-text-muted mt-2">No trend data yet</p>
            </div>
          )}
          <PeopleRanking people={people?.people ?? []} />
        </div>
      )}

      {/* Health alerts */}
      {health && (
        <HealthAlerts
          indicators={health.indicators}
          loadLabel={health.load_score.label}
        />
      )}
    </div>
  );
}
