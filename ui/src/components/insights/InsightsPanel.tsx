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

type Period = "daily" | "weekly" | "monthly";

const PERIOD_OPTIONS: { value: Period; label: string }[] = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

export function InsightsPanel() {
  const [period, setPeriod] = useState<Period>("weekly");

  const { data: summary } = useQuery({
    queryKey: ["analytics-summary", period],
    queryFn: () => getAnalyticsSummary(period),
  });
  const { data: trends } = useQuery({
    queryKey: ["analytics-trends", period],
    queryFn: () => getAnalyticsTrends(period, 8),
  });
  const { data: people } = useQuery({
    queryKey: ["analytics-people"],
    queryFn: () => getAnalyticsPeople(10),
  });
  const { data: health } = useQuery({
    queryKey: ["analytics-health"],
    queryFn: getAnalyticsHealth,
  });

  const current = summary?.current_period;

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-lg font-semibold text-text-primary">Insights</h1>
        <div className="flex gap-1 bg-surface border border-border rounded-lg p-0.5">
          {PERIOD_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setPeriod(opt.value)}
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
      <div className="grid grid-cols-4 gap-3 mb-6">
        <StatCard label="Meetings" value={current?.total_meetings ?? 0} />
        <StatCard
          label="Hours"
          value={current ? Math.round(current.total_duration_minutes / 60) : 0}
        />
        <StatCard label="Load" value={health?.load_score.label ?? "N/A"} />
        <StatCard label="Attendees" value={current?.unique_attendees ?? 0} />
      </div>

      {/* Trend + People row */}
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
