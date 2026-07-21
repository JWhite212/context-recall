import { useState } from "react";

export interface LinkCandidate {
  id: string;
  label: string;
  subtitle: string;
}

interface CalendarLinkPickerProps {
  title: string;
  candidates: LinkCandidate[];
  emptyLabel: string;
  onPick: (id: string) => void;
  onClose: () => void;
  busy?: boolean;
}

export function CalendarLinkPicker({
  title,
  candidates,
  emptyLabel,
  onPick,
  onClose,
  busy = false,
}: CalendarLinkPickerProps) {
  const [q, setQ] = useState("");
  const filtered = candidates.filter((c) =>
    c.label.toLowerCase().includes(q.trim().toLowerCase()),
  );

  return (
    <div
      className="absolute z-20 mt-1 w-64 rounded-lg border border-border bg-surface-raised p-3 shadow-lg text-xs"
      role="dialog"
      aria-label={title}
    >
      <div className="flex items-center justify-between mb-2">
        <span className="font-medium text-text-primary">{title}</span>
        <button
          type="button"
          onClick={onClose}
          className="text-text-muted hover:text-text-secondary"
          aria-label="Close"
        >
          ✕
        </button>
      </div>
      <input
        type="text"
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Search…"
        className="w-full mb-2 px-2 py-1 rounded border border-border bg-surface text-text-primary"
      />
      {filtered.length === 0 ? (
        <p className="text-text-muted py-2">{emptyLabel}</p>
      ) : (
        <ul className="flex flex-col gap-0.5 max-h-56 overflow-auto">
          {filtered.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                disabled={busy}
                onClick={() => onPick(c.id)}
                className="w-full text-left px-2 py-1 rounded hover:bg-surface-hover disabled:opacity-50"
              >
                <span className="block text-text-primary truncate">
                  {c.label}
                </span>
                <span className="block text-[10px] text-text-muted">
                  {c.subtitle}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
