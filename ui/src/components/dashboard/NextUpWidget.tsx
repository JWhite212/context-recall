import { useEffect, useMemo, useState } from "react";
import { format } from "date-fns";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import type { CalendarEvent } from "../../lib/types";
import {
  getCalendarEvents,
  getPreparedEventUids,
  generatePrepForEvent,
  startRecording,
} from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useToast } from "../common/Toast";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import { PrepModal } from "../calendar/PrepModal";

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
  // The 60s refetch can lag start_ts by a moment — "in 0 min" reads as a
  // glitch, so bridge the gap until happeningNow takes over.
  if (mins === 0) return "starting now";
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
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const toast = useToast();

  const [, setTick] = useState(0);
  const [showPrep, setShowPrep] = useState(false);
  const [confirmingRecord, setConfirmingRecord] = useState(false);

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

  const event: CalendarEvent | undefined = useMemo(
    () =>
      (data?.events ?? [])
        .filter((e) => e.end_ts >= nowSec)
        .sort((a, b) => a.start_ts - b.start_ts)[0],
    [data, nowSec],
  );

  // Live countdown tick — only while there is actually a countdown to show
  // (final-review Minor: an unconditional interval forced a re-render every
  // second even when the widget rendered null/empty/error).
  const hasHero = daemonRunning && event !== undefined;
  useEffect(() => {
    if (!hasHero) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [hasHero]);

  const generate = useMutation({
    mutationFn: () => {
      if (!event) throw new Error("no event");
      return generatePrepForEvent({
        event_uid: event.event_uid,
        title: event.title || "Untitled",
        attendees: event.attendees,
        attendee_names: event.attendees.map((a) => a.name || a.email),
        end_ts: event.end_ts,
        series_id: null,
      });
    },
    onSuccess: (dataPrep) => {
      if (!event) return;
      queryClient.setQueryData(["prep", "by-event", event.event_uid], dataPrep);
      void queryClient.invalidateQueries({ queryKey: ["prepared-events"] });
      setShowPrep(true);
    },
    onError: () => toast.error("Failed to generate prep."),
  });

  const record = useMutation({
    mutationFn: () => startRecording(),
    onSuccess: () => setConfirmingRecord(false),
    onError: () => toast.error("Failed to start recording."),
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
  const isRecording = state === "recording";
  const live = event.start_ts - 300 <= nowSec && nowSec <= event.end_ts;

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

        <div className="mt-1 flex flex-wrap items-center gap-3 text-xs border-t border-border pt-3">
          {prepared && (
            <button
              type="button"
              onClick={() => setShowPrep(true)}
              className="text-accent hover:underline"
            >
              View prep
            </button>
          )}
          <button
            type="button"
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
            className="text-accent hover:underline disabled:opacity-50"
          >
            {generate.isPending
              ? "Generating..."
              : prepared
                ? "Regenerate prep"
                : "Generate prep"}
          </button>

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

          {!confirmingRecord ? (
            <button
              type="button"
              onClick={() => setConfirmingRecord(true)}
              disabled={!live || isRecording}
              title={
                isRecording
                  ? "Already recording"
                  : live
                    ? ""
                    : "Available when the meeting is live"
              }
              className="text-accent hover:underline disabled:opacity-40 disabled:no-underline disabled:text-text-muted"
            >
              Record this meeting
            </button>
          ) : (
            <span className="flex items-center gap-2">
              <button
                type="button"
                onClick={() => record.mutate()}
                disabled={record.isPending}
                className="text-accent hover:underline disabled:opacity-50"
              >
                Start recording?
              </button>
              <button
                type="button"
                onClick={() => setConfirmingRecord(false)}
                className="text-text-muted hover:text-text-secondary"
              >
                Cancel
              </button>
            </span>
          )}

          <button
            type="button"
            onClick={() => navigate("/calendar")}
            className="ml-auto text-text-muted hover:text-text-secondary"
          >
            Open in calendar
          </button>
        </div>
      </div>

      {showPrep && (
        <PrepModal
          eventUid={event.event_uid}
          title={title}
          onClose={() => setShowPrep(false)}
        />
      )}
    </Shell>
  );
}
