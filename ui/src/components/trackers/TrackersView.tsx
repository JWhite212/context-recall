import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  createTracker,
  deleteTracker,
  getTrackerHits,
  getTrackers,
  updateTracker,
} from "../../lib/api";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonCard } from "../common/Skeleton";
import { useToast } from "../common/Toast";
import type { Tracker } from "../../lib/types";

function TrackerHits({ tracker }: { tracker: Tracker }) {
  const navigate = useNavigate();
  const { data: hits = [], isLoading } = useQuery({
    queryKey: ["tracker-hits", tracker.id],
    queryFn: () => getTrackerHits(tracker.id),
  });

  if (isLoading) {
    return (
      <p className="text-xs text-text-muted px-3 pb-2">Loading mentions…</p>
    );
  }
  if (hits.length === 0) {
    return (
      <p className="text-xs text-text-muted px-3 pb-2">
        No mentions yet — new meetings are scanned automatically.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-1 px-3 pb-2">
      {hits.slice(0, 20).map((h) => (
        <button
          key={h.id}
          onClick={() => navigate(`/meetings/${h.meeting_id}`)}
          className="text-left py-1 px-2 rounded-lg hover:bg-sidebar-hover cursor-pointer"
        >
          <span className="text-xs font-medium text-text-primary">
            {h.meeting_title || "Meeting"}
            {h.meeting_started_at
              ? ` — ${new Date(h.meeting_started_at * 1000).toLocaleDateString()}`
              : ""}
          </span>
          <span className="text-xs text-text-muted block truncate">
            “{h.matched_text}”
          </span>
        </button>
      ))}
      {hits.length > 20 && (
        <p className="text-xs text-text-muted px-2">
          …and {hits.length - 20} more
        </p>
      )}
    </div>
  );
}

function TrackerRow({ tracker }: { tracker: Tracker }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);

  const toggle = useMutation({
    mutationFn: () => updateTracker(tracker.id, { enabled: !tracker.enabled }),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["trackers"] }),
    onError: () => toast.error("Failed to update tracker"),
  });

  const remove = useMutation({
    mutationFn: () => deleteTracker(tracker.id),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: ["trackers"] }),
    onError: () => toast.error("Failed to delete tracker"),
  });

  return (
    <div className="rounded-lg hover:bg-sidebar-hover transition-colors">
      <div className="flex items-center justify-between py-2 px-3">
        <button
          onClick={() => setExpanded(!expanded)}
          className="min-w-0 flex items-center gap-2 text-left cursor-pointer"
        >
          <p className="text-sm font-medium text-text-primary truncate">
            {tracker.name}
          </p>
          <span className="text-xs text-text-muted truncate">
            {tracker.keywords.join(", ")}
          </span>
          {!tracker.enabled && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-surface text-text-muted">
              paused
            </span>
          )}
        </button>
        <div className="flex items-center gap-3 ml-3 whitespace-nowrap">
          <button
            onClick={() => toggle.mutate()}
            className="text-xs text-text-muted hover:text-text-primary hover:underline cursor-pointer"
          >
            {tracker.enabled ? "Pause" : "Resume"}
          </button>
          <button
            onClick={() => {
              if (
                window.confirm(
                  `Delete tracker "${tracker.name}" and its history?`,
                )
              ) {
                remove.mutate();
              }
            }}
            className="text-xs text-rose-400 hover:underline cursor-pointer"
          >
            Delete
          </button>
        </div>
      </div>
      {expanded && <TrackerHits tracker={tracker} />}
    </div>
  );
}

export function TrackersView() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [name, setName] = useState("");
  const [keywords, setKeywords] = useState("");

  const {
    data: trackers = [],
    isLoading,
    isError,
    refetch,
  } = useQuery({
    queryKey: ["trackers"],
    queryFn: getTrackers,
  });

  const add = useMutation({
    mutationFn: () =>
      createTracker({
        name: name.trim(),
        keywords: keywords
          .split(",")
          .map((k) => k.trim())
          .filter((k) => k.length >= 2),
      }),
    onSuccess: () => {
      setName("");
      setKeywords("");
      void queryClient.invalidateQueries({ queryKey: ["trackers"] });
    },
    onError: () => toast.error("Failed to create tracker"),
  });

  const canAdd =
    name.trim().length > 0 &&
    keywords.split(",").some((k) => k.trim().length >= 2) &&
    !add.isPending;

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      <div className="flex items-center gap-2">
        <h1 className="text-lg font-semibold text-text-primary">Trackers</h1>
        {!isLoading && !isError && (
          <span className="text-xs text-text-muted">({trackers.length})</span>
        )}
      </div>

      <p className="text-xs text-text-muted">
        Watch for topics across every meeting — competitor names, "pricing", a
        project codename. Each new transcript is scanned automatically and
        mentions collect here.
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (canAdd) add.mutate();
        }}
        className="flex gap-2"
      >
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Tracker name, e.g. Pricing talk"
          className="w-56 px-3 py-2 text-sm rounded-lg bg-surface-raised border border-border text-text-primary"
        />
        <input
          value={keywords}
          onChange={(e) => setKeywords(e.target.value)}
          placeholder="Keywords, comma-separated: pricing, discount, renewal"
          className="flex-1 px-3 py-2 text-sm rounded-lg bg-surface-raised border border-border text-text-primary"
        />
        <button
          type="submit"
          disabled={!canAdd}
          className="text-sm px-4 py-2 rounded-lg bg-accent text-white disabled:opacity-50 cursor-pointer"
        >
          Add
        </button>
      </form>

      {isLoading ? (
        <div className="rounded-xl bg-surface-raised border border-border p-6">
          <div className="flex flex-col gap-2">
            {Array.from({ length: 2 }).map((_, i) => (
              <SkeletonCard key={i} />
            ))}
          </div>
        </div>
      ) : isError ? (
        <ErrorState
          message="Failed to load trackers."
          onRetry={() => refetch()}
        />
      ) : trackers.length === 0 ? (
        <EmptyState
          title="No trackers yet"
          description="Add keywords you care about and Context Recall will flag every meeting where they come up."
        />
      ) : (
        <div className="rounded-xl bg-surface-raised border border-border p-3">
          <div className="flex flex-col gap-1">
            {trackers.map((t) => (
              <TrackerRow key={t.id} tracker={t} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
