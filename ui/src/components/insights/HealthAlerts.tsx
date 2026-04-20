interface Props {
  indicators: string[];
  loadLabel: string;
}

export function HealthAlerts({ indicators, loadLabel }: Props) {
  const displayLabel = loadLabel || "No data";

  if (indicators.length === 0 && !loadLabel) return null;

  const badgeColor =
    displayLabel === "Overloaded"
      ? "bg-red-500/20 text-red-400"
      : displayLabel === "Heavy"
        ? "bg-orange-500/20 text-orange-400"
        : "bg-green-500/20 text-green-400";

  return (
    <div className="p-4 bg-surface-raised border border-border rounded-lg">
      <div className="flex items-center gap-2 mb-2">
        <p className="text-xs text-text-muted">Meeting Health</p>
        <span
          className={`text-xs px-2 py-0.5 rounded-full font-medium ${badgeColor}`}
        >
          {displayLabel}
        </span>
      </div>
      {indicators.length > 0 && (
        <ul className="space-y-1">
          {indicators.map((msg, i) => (
            <li key={i} className="text-sm text-text-secondary">
              {msg}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
