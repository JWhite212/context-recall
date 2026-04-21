import { useQuery } from "@tanstack/react-query";
import { getAnalyticsHealth } from "../../lib/api";

export function HealthSummary() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["analytics-health"],
    queryFn: getAnalyticsHealth,
  });

  if (!data || isLoading || isError) return null;

  const label = data.load_score.label;
  if (label === "Normal" || label === "Light week") return null;

  const badgeColor =
    label === "Overloaded"
      ? "bg-red-500/20 text-red-400"
      : label === "Heavy"
        ? "bg-orange-500/20 text-orange-400"
        : "bg-gray-500/20 text-gray-400";

  return (
    <div className="rounded-xl bg-surface-raised border border-border p-6">
      <div className="flex items-center gap-2 mb-2">
        <p className="text-xs text-text-muted">Meeting Health</p>
        <span
          className={`text-xs px-2 py-0.5 rounded-full font-medium ${badgeColor}`}
        >
          {label}
        </span>
      </div>
      {data.indicators.length > 0 && (
        <ul className="space-y-1">
          {data.indicators.map((msg, i) => (
            <li key={i} className="text-sm text-text-secondary">
              {msg}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
