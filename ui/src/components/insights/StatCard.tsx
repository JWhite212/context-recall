interface Props {
  label: string;
  value: string | number;
  delta?: number;
}

export function StatCard({ label, value, delta }: Props) {
  return (
    <div className="p-4 bg-surface-raised border border-border rounded-lg">
      <p className="text-xs text-text-muted">{label}</p>
      <div className="flex items-baseline gap-2 mt-1">
        <span className="text-xl font-semibold text-text-primary">{value}</span>
        {delta !== undefined && delta !== 0 && (
          <span
            className={`text-xs font-medium ${delta > 0 ? "text-orange-400" : "text-green-400"}`}
          >
            {delta > 0 ? "\u2191" : "\u2193"}
            {Math.abs(delta)}%
          </span>
        )}
      </div>
    </div>
  );
}
