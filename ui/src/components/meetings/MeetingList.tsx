import { useState, useRef, useDeferredValue } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { useVirtualizer } from "@tanstack/react-virtual";
import { getMeetings, mergeMeetings } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { SkeletonMeetingRow } from "../common/Skeleton";
import { useToast } from "../common/Toast";
import type { Meeting, MeetingStatus } from "../../lib/types";

const STATUS_FILTERS: { label: string; value: MeetingStatus | "all" }[] = [
  { label: "All", value: "all" },
  { label: "Complete", value: "complete" },
  { label: "Recording", value: "recording" },
  { label: "Error", value: "error" },
];

const PAGE_SIZE = 100;
const ROW_HEIGHT = 72;

export function MeetingList() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { daemonRunning } = useDaemonStatus();
  const toast = useToast();
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);
  const [statusFilter, setStatusFilter] = useState<MeetingStatus | "all">(
    "all",
  );
  const [page, setPage] = useState(0);
  const listRef = useRef<HTMLDivElement>(null);
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: ["meetings", statusFilter, page, deferredSearch],
    queryFn: () =>
      getMeetings(
        PAGE_SIZE,
        page * PAGE_SIZE,
        deferredSearch || undefined,
        statusFilter !== "all" ? statusFilter : undefined,
      ),
    enabled: daemonRunning,
    staleTime: 10_000,
    refetchInterval: 10_000,
  });

  const merge = useMutation({
    mutationFn: (ids: string[]) => mergeMeetings(ids),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      setSelectMode(false);
      setSelected(new Set());
      toast.success(`Merged into "${data.title}".`);
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : "Failed to merge meetings.",
      );
    },
  });

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const meetings = data?.meetings ?? [];
  const total = data?.total ?? 0;
  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      <h1 className="text-lg font-semibold text-text-primary">Meetings</h1>

      {!daemonRunning ? (
        <p className="text-sm text-text-muted">
          Start the daemon to view meeting history.
        </p>
      ) : (
        <>
          {/* Search + filters */}
          <div className="flex items-center gap-3">
            <input
              type="text"
              placeholder="Search meetings..."
              aria-label="Search meetings"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(0);
              }}
              className="flex-1 px-3 py-1.5 text-sm rounded-lg bg-surface-raised border border-border text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-accent"
            />
            <button
              onClick={() => {
                setSelectMode((v) => !v);
                setSelected(new Set());
              }}
              aria-pressed={selectMode}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
                selectMode
                  ? "bg-accent text-white border-accent"
                  : "bg-surface-raised border-border text-text-secondary hover:bg-sidebar-hover"
              }`}
            >
              {selectMode ? "Cancel" : "Select"}
            </button>
          </div>

          {/* Merge bar */}
          {selectMode && (
            <div className="flex items-center gap-3">
              <span className="text-xs text-text-muted">
                {selected.size} selected
              </span>
              {selected.size >= 2 && (
                <button
                  onClick={() => merge.mutate(Array.from(selected))}
                  disabled={merge.isPending}
                  className="px-3 py-1 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
                >
                  {merge.isPending ? "Merging..." : "Merge Selected"}
                </button>
              )}
            </div>
          )}

          <div className="flex gap-1.5">
            {STATUS_FILTERS.map((f) => (
              <button
                key={f.value}
                onClick={() => {
                  setStatusFilter(f.value);
                  setPage(0);
                }}
                aria-label={`Filter by ${f.label}`}
                aria-pressed={statusFilter === f.value}
                className={`px-3 py-1 text-xs rounded-full transition-colors ${
                  statusFilter === f.value
                    ? "bg-accent text-white"
                    : "bg-surface-raised text-text-secondary hover:bg-sidebar-hover"
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>

          {/* Meeting list */}
          {isLoading ? (
            <div role="status" aria-label="Loading meetings">
              <div className="flex flex-col gap-1">
                {Array.from({ length: 5 }).map((_, i) => (
                  <SkeletonMeetingRow key={i} />
                ))}
              </div>
            </div>
          ) : isError ? (
            <ErrorState
              message="Failed to load meetings."
              onRetry={() => refetch()}
            />
          ) : meetings.length === 0 ? (
            <EmptyState
              title={search ? "No results" : "No meetings yet"}
              description={
                search
                  ? `No meetings match "${search}".`
                  : "Meetings will appear here once the daemon records them."
              }
            />
          ) : (
            <VirtualMeetingList
              meetings={meetings}
              onSelect={(id) => navigate(`/meetings/${id}`)}
              listRef={listRef}
              selectMode={selectMode}
              selected={selected}
              onToggleSelect={toggleSelect}
            />
          )}

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between pt-2">
              <span className="text-xs text-text-muted">
                {total} meeting{total !== 1 ? "s" : ""}
              </span>
              <div className="flex gap-1">
                <button
                  disabled={page === 0}
                  onClick={() => setPage((p) => p - 1)}
                  aria-label="Previous page"
                  className="px-3 py-1 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  Prev
                </button>
                <span className="px-2 py-1 text-xs text-text-muted">
                  {page + 1} / {totalPages}
                </span>
                <button
                  disabled={page >= totalPages - 1}
                  onClick={() => setPage((p) => p + 1)}
                  aria-label="Next page"
                  className="px-3 py-1 text-xs rounded-lg bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function VirtualMeetingList({
  meetings,
  onSelect,
  listRef,
  selectMode,
  selected,
  onToggleSelect,
}: {
  meetings: Meeting[];
  onSelect: (id: string) => void;
  listRef: React.RefObject<HTMLDivElement | null>;
  selectMode: boolean;
  selected: Set<string>;
  onToggleSelect: (id: string) => void;
}) {
  const virtualizer = useVirtualizer({
    count: meetings.length,
    getScrollElement: () => listRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 5,
  });

  // For small lists, skip virtualisation overhead.
  if (meetings.length <= 30) {
    return (
      <div className="flex flex-col gap-1" role="list">
        {meetings.map((m) => (
          <div key={m.id} role="listitem">
            <MeetingRow
              meeting={m}
              onSelect={onSelect}
              selectMode={selectMode}
              isSelected={selected.has(m.id)}
              onToggleSelect={onToggleSelect}
            />
          </div>
        ))}
      </div>
    );
  }

  return (
    <div ref={listRef} className="max-h-[60vh] overflow-y-auto">
      <div
        className="relative"
        role="list"
        style={{ height: virtualizer.getTotalSize() }}
      >
        {virtualizer.getVirtualItems().map((virtual) => {
          const m = meetings[virtual.index];
          return (
            <div
              key={m.id}
              role="listitem"
              className="absolute left-0 right-0"
              style={{
                top: virtual.start,
                height: virtual.size,
              }}
            >
              <MeetingRow
                meeting={m}
                onSelect={onSelect}
                selectMode={selectMode}
                isSelected={selected.has(m.id)}
                onToggleSelect={onToggleSelect}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function MeetingRow({
  meeting: m,
  onSelect,
  selectMode,
  isSelected,
  onToggleSelect,
}: {
  meeting: Meeting;
  onSelect: (id: string) => void;
  selectMode: boolean;
  isSelected: boolean;
  onToggleSelect: (id: string) => void;
}) {
  return (
    <button
      onClick={() => (selectMode ? onToggleSelect(m.id) : onSelect(m.id))}
      className={`flex items-center justify-between py-3 px-4 rounded-xl bg-surface-raised border transition-colors text-left w-full mb-1 ${
        isSelected
          ? "border-accent/60 bg-accent/5"
          : "border-border hover:border-accent/40"
      }`}
    >
      {selectMode && (
        <div className="shrink-0 mr-3 flex items-center">
          <input
            type="checkbox"
            checked={isSelected}
            onChange={() => onToggleSelect(m.id)}
            onClick={(e) => e.stopPropagation()}
            className="h-4 w-4 rounded border-border text-accent focus:ring-accent"
          />
        </div>
      )}

      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-text-primary truncate">
          {m.title}
        </p>
        <div className="flex items-center gap-2 mt-0.5">
          <span className="text-xs text-text-muted">
            {new Date(m.started_at * 1000).toLocaleDateString(undefined, {
              month: "short",
              day: "numeric",
              year: "numeric",
            })}{" "}
            {new Date(m.started_at * 1000).toLocaleTimeString(undefined, {
              hour: "2-digit",
              minute: "2-digit",
            })}
          </span>
          {m.duration_seconds != null && (
            <span className="text-xs text-text-muted">
              {Math.round(m.duration_seconds / 60)}m
            </span>
          )}
          {m.word_count != null && (
            <span className="text-xs text-text-muted">
              {m.word_count.toLocaleString()} words
            </span>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 shrink-0 ml-4">
        {m.label && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-purple-500/20 text-purple-400">
            {m.label}
          </span>
        )}
        {m.tags.length > 0 && (
          <div className="flex gap-1">
            {m.tags.slice(0, 2).map((tag) => (
              <span
                key={tag}
                className="text-[10px] px-1.5 py-0.5 rounded bg-accent/10 text-accent"
              >
                {tag}
              </span>
            ))}
          </div>
        )}
        <span
          className={`text-xs px-2 py-0.5 rounded-full ${
            m.status === "complete"
              ? "bg-status-idle/20 text-status-idle"
              : m.status === "error"
                ? "bg-status-error/20 text-status-error"
                : "bg-blue-400/20 text-blue-400"
          }`}
        >
          {m.status}
        </span>
      </div>
    </button>
  );
}
