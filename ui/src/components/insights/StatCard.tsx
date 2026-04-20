interface Props {
  label: string;
  value: string | number;
  delta?: number;
  lowerIsBetter?: boolean;
}

export function StatCard({
  label,
  value,
  delta,
  lowerIsBetter = false,
}: Props) {
  const showDelta = delta !== undefined && !Number.isNaN(delta) && delta !== 0;
  const isNegative = lowerIsBetter ? delta! > 0 : delta! < 0;

  return (
    <div className="p-4 bg-surface-raised border border-border rounded-lg">
      <p className="text-xs text-text-muted">{label}</p>
      <div className="flex items-baseline gap-2 mt-1">
        <span className="text-xl font-semibold text-text-primary">{value}</span>
        {showDelta && (
          <span
            className={`text-xs font-medium ${isNegative ? "text-red-400" : "text-green-400"}`}
          >
            {delta! > 0 ? "\u2191" : "\u2193"}
            {Math.abs(delta!)}%
          </span>
        )}
      </div>
    </div>
  );
}
