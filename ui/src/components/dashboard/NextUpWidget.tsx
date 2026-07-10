import { useEffect, useState } from "react";
import { format } from "date-fns";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { CalendarEvent } from "../../lib/types";
import { getCalendarEvents, getPreparedEventUids } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";

const DAY_SECONDS = 86_400;

function providerLabel(joinUrl: string): string {
  const u = joinUrl.toLowerCase();
  if (u.includes("teams.")) return "Teams";
  if (u.includes("zoom.")) return "Zoom";
  if (u.includes("meet.google")) return "Meet";
  return "Video call";
}

function relativeLabel(startSec: number, nowSec: number): string {
  const mins = Math.max(0, Math.round((startSec - nowSec) / 60));
  if (mins < 60) return `in ${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `in ${h}h ${m}m` : `in ${h}h`;
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-xl bg-surface-raised border border-border p-6">
      <h2 className="text-sm font-medium text-text-primary mb-4">Next up</h2>
      {children}
    </div>
  );
}

export function NextUpWidget() {
  const { daemonRunning, state } = useDaemonStatus();
  void state;
  const navigate = useNavigate();

  // Re-render every second so the relative countdown stays live.
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const nowSec = Date.now() / 1000;

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["calendar", "next-up"],
    queryFn: () =>
      getCalendarEvents(Math.floor(nowSec), Math.floor(nowSec) + DAY_SECONDS),
    enabled: daemonRunning,
    refetchInterval: 60_000,
  });

  const { data: preparedData } = useQuery({
    queryKey: ["prepared-events"],
    queryFn: getPreparedEventUids,
    enabled: daemonRunning,
  });

  if (!daemonRunning) return null;

  if (isLoading) {
    return (
      <Shell>
        <div role="status" aria-label="Loading next meeting">
          <SkeletonCard />
        </div>
      </Shell>
    );
  }

  if (isError) {
    return (
      <Shell>
        <ErrorState
          message="Failed to load your calendar."
          onRetry={() => refetch()}
        />
      </Shell>
    );
  }

  const event: CalendarEvent | undefined = (data?.events ?? [])
    .filter((e) => e.end_ts >= nowSec)
    .sort((a, b) => a.start_ts - b.start_ts)[0];

  if (!event) {
    return (
      <Shell>
        <EmptyState title="Nothing scheduled" description="in the next 24h" />
      </Shell>
    );
  }

  const happeningNow = event.start_ts <= nowSec && nowSec <= event.end_ts;
  const prepared = new Set(preparedData?.event_uids ?? []).has(event.event_uid);
  const title = event.title || "Untitled";
  const startedMins = Math.max(0, Math.round((nowSec - event.start_ts) / 60));

  const metaParts: string[] = [];
  if (event.attendees.length > 0) {
    metaParts.push(
      `${event.attendees.length} attendee${event.attendees.length === 1 ? "" : "s"}`,
    );
  }
  if (event.join_url) metaParts.push(providerLabel(event.join_url));

  return (
    <Shell>
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2 text-xs">
          {happeningNow ? (
            <span className="flex items-center gap-1.5 text-status-recording">
              <span className="w-2 h-2 rounded-full bg-status-recording animate-pulse" />
              Happening now
              <span className="text-text-muted">
                · started {startedMins}m ago
              </span>
            </span>
          ) : (
            <span className="text-accent">
              {relativeLabel(event.start_ts, nowSec)}
              <span className="text-text-muted">
                {" "}
                · {format(new Date(event.start_ts * 1000), "HH:mm")}
              </span>
            </span>
          )}
          {prepared && (
            <span className="ml-auto rounded bg-accent/20 text-accent px-1.5 py-0.5 text-[10px]">
              Prep ready
            </span>
          )}
        </div>

        <p className="text-base font-medium text-text-primary">{title}</p>

        {metaParts.length > 0 && (
          <p className="text-xs text-text-muted">{metaParts.join(" · ")}</p>
        )}

        <div className="mt-1 flex items-center gap-3 text-xs">
          {event.join_url && (
            <a
              href={event.join_url}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Join
            </a>
          )}
          <button
            type="button"
            onClick={() => navigate("/calendar")}
            className="text-text-muted hover:text-text-secondary"
          >
            Open in calendar
          </button>
        </div>
      </div>
    </Shell>
  );
}
