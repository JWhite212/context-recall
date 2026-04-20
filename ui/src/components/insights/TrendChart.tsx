import { format, parseISO } from "date-fns";
import type { AnalyticsPeriod } from "../../lib/types";

interface Props {
  periods: AnalyticsPeriod[];
  metricKey: keyof Pick<
    AnalyticsPeriod,
    | "total_meetings"
    | "total_duration_minutes"
    | "total_words"
    | "unique_attendees"
  >;
  label: string;
}

export function TrendChart({ periods, metricKey, label }: Props) {
  const values = periods.map((p) => p[metricKey] as number);
  const max = Math.max(...values, 1);

  return (
    <div className="p-4 bg-surface-raised border border-border rounded-lg">
      <p className="text-xs text-text-muted mb-2">{label}</p>
      <div className="h-24 flex items-end gap-1">
        {periods.map((period) => {
          const val = period[metricKey] as number;
          const pct = (val / max) * 100;
          return (
            <div
              key={period.id}
              className="flex-1 flex flex-col items-center justify-end h-full"
            >
              <div
                className="w-full bg-accent/60 rounded-t transition-all"
                style={{ height: `${pct}%` }}
                title={`${val}`}
              />
              <span className="text-[10px] text-text-muted mt-1 truncate w-full text-center">
                {format(parseISO(period.period_start), "M/d")}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
