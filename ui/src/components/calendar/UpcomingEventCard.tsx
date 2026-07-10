import { useState } from "react";
import { format } from "date-fns";
import type { CalendarEvent } from "../../lib/types";

interface UpcomingEventCardProps {
  event: CalendarEvent;
  compact?: boolean;
  preparedUids?: Set<string>;
}

/** Renders an imported (not-yet-recorded) calendar event, distinct from recorded meetings. */
export function UpcomingEventCard({
  event,
  compact = false,
  preparedUids,
}: UpcomingEventCardProps) {
  const [open, setOpen] = useState(false);
  const title = event.title || "Untitled";
  const start = format(new Date(event.start_ts * 1000), "HH:mm");
  const prepared = preparedUids?.has(event.event_uid) ?? false;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label={title}
        className={`w-full text-left rounded border border-dashed border-text-muted/40 bg-surface-hover/40 text-text-secondary hover:border-accent/50 transition-colors ${
          compact ? "px-1 py-0.5 text-[10px]" : "px-2 py-1 text-xs"
        }`}
      >
        <span className="truncate block">
          {!compact && <span className="text-text-muted mr-1">{start}</span>}
          {title}
          {prepared && (
            <span className="ml-1 rounded bg-accent/20 text-accent px-1 text-[9px] align-middle">
              Prep ready
            </span>
          )}
        </span>
      </button>
      {open && (
        <div className="absolute z-10 mt-1 w-56 rounded-lg border border-border bg-surface-raised p-3 shadow-lg text-xs">
          <p className="font-medium text-text-primary">{title}</p>
          <p className="text-text-muted mt-0.5">
            {format(new Date(event.start_ts * 1000), "EEE d MMM, HH:mm")} –{" "}
            {format(new Date(event.end_ts * 1000), "HH:mm")}
          </p>
          {event.attendees.length > 0 && (
            <ul className="mt-2 flex flex-col gap-0.5">
              {event.attendees.map((a) => (
                <li key={a.email || a.name} className="text-text-secondary">
                  {a.name || a.email}
                </li>
              ))}
            </ul>
          )}
          {event.join_url && (
            <a
              href={event.join_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-accent hover:underline"
            >
              Join
            </a>
          )}
        </div>
      )}
    </div>
  );
}
