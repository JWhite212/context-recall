/** Small inline badge showing which summary template a meeting used. */
export function TemplateBadge({
  name,
  source,
}: {
  name?: string | null;
  source?: string | null;
}) {
  if (!name) return null;
  return (
    <span
      className="text-xs text-text-muted inline-flex items-center gap-1"
      title={`Summary template (${source || "auto"})`}
    >
      Template: <span className="text-text-secondary">{name}</span>
      {source === "manual" && (
        <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-green-400/20 text-green-400">
          manual
        </span>
      )}
    </span>
  );
}
