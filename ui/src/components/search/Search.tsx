import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  searchTranscripts,
  reindexMeetings,
  getMeetingLabels,
} from "../../lib/api";
import type { SearchResult } from "../../lib/types";
import { useToast } from "../common/Toast";
import { Skeleton, SkeletonLine } from "../common/Skeleton";

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

const EXAMPLE_QUERIES = [
  "action items",
  "decisions made",
  "next steps",
  "blockers",
];

export function Search() {
  const navigate = useNavigate();
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [isSearching, setIsSearching] = useState(false);
  const [hasSearched, setHasSearched] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const { data: labels = [] } = useQuery({
    queryKey: ["meeting-labels"],
    queryFn: getMeetingLabels,
    staleTime: 30_000,
  });

  const reindex = useMutation({
    mutationFn: reindexMeetings,
    onSuccess: (data) => {
      toast.success(
        `Re-indexed ${data.meetings_indexed} meeting${data.meetings_indexed !== 1 ? "s" : ""} (${data.segments_indexed} segments).`,
      );
    },
    onError: (err) => {
      toast.error(
        err instanceof Error ? err.message : "Failed to re-index meetings.",
      );
    },
  });

  // Debounced search
  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setResults([]);
      setHasSearched(false);
      return;
    }

    const timer = setTimeout(() => {
      setIsSearching(true);
      searchTranscripts(trimmed)
        .then((res) => {
          setResults(res.results);
          setHasSearched(true);
        })
        .catch(() => {
          setResults([]);
          setHasSearched(true);
        })
        .finally(() => setIsSearching(false));
    }, 300);

    return () => clearTimeout(timer);
  }, [query]);

  // Auto-focus input on mount
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div className="flex flex-col gap-4 p-6 max-w-3xl">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-text-primary">Search</h1>
        <button
          onClick={() => reindex.mutate()}
          disabled={reindex.isPending}
          className="text-xs text-text-muted hover:text-text-secondary underline disabled:opacity-50"
        >
          {reindex.isPending ? "Re-indexing..." : "Re-index all meetings"}
        </button>
      </div>

      {/* Search input */}
      <div className="relative">
        <svg
          className="absolute left-3 top-1/2 -translate-y-1/2 text-text-muted"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="8" />
          <line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          ref={inputRef}
          type="text"
          placeholder="Search across all meeting transcripts..."
          aria-label="Search transcripts"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full pl-10 pr-3 py-2 rounded-md bg-surface border border-border text-text-primary text-sm placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-accent"
        />
      </div>

      {/* Loading state */}
      {isSearching && (
        <div
          className="flex flex-col gap-2"
          role="status"
          aria-label="Searching transcripts"
        >
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="flex flex-col gap-1.5 py-3 px-4 rounded-xl bg-surface-raised border border-border"
              aria-hidden="true"
            >
              <div className="flex items-center justify-between gap-2">
                <SkeletonLine width="w-48" />
                <Skeleton className="h-3 w-10 rounded" />
              </div>
              <SkeletonLine width="w-full" />
              <SkeletonLine width="w-2/3" />
              <div className="flex items-center gap-2">
                <Skeleton className="h-3 w-16" />
                <Skeleton className="h-3 w-12" />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Results */}
      {!isSearching && hasSearched && results.length === 0 && (
        <div className="flex flex-col items-center justify-center py-12 text-center">
          <p className="text-sm text-text-secondary">No results found</p>
          <p className="text-xs text-text-muted mt-1">
            Try a different search term or re-index your meetings.
          </p>
        </div>
      )}

      {!isSearching && !hasSearched && !query.trim() && (
        <div className="flex flex-col items-center justify-center py-12 text-center gap-4">
          <div>
            <p className="text-sm text-text-secondary">
              Search across all your meeting transcripts
            </p>
            <p className="text-xs text-text-muted mt-1">
              Type a query above to find relevant transcript segments.
            </p>
          </div>

          {labels.length > 0 && (
            <div className="flex flex-col items-center gap-1.5">
              <span className="text-xs text-text-muted">
                Try searching by label:
              </span>
              <div className="flex flex-wrap justify-center gap-1.5">
                {labels.slice(0, 8).map((label) => (
                  <button
                    key={label}
                    onClick={() => setQuery(label)}
                    className="text-xs px-2.5 py-1 rounded-full bg-accent/10 text-accent hover:bg-accent/20 transition-colors"
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="flex flex-col items-center gap-1.5">
            <span className="text-xs text-text-muted">Example queries:</span>
            <div className="flex flex-wrap justify-center gap-1.5">
              {EXAMPLE_QUERIES.map((example) => (
                <button
                  key={example}
                  onClick={() => setQuery(example)}
                  className="text-xs px-2.5 py-1 rounded-full bg-surface-raised border border-border text-text-secondary hover:bg-sidebar-hover transition-colors"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      {!isSearching && results.length > 0 && (
        <div className="flex flex-col gap-2" role="list">
          {results.map((result, idx) => (
            <button
              key={`${result.meeting_id}-${result.segment_index}-${idx}`}
              role="listitem"
              onClick={() => navigate(`/meetings/${result.meeting_id}`)}
              className="flex flex-col gap-1.5 py-3 px-4 rounded-xl bg-surface-raised border border-border hover:border-accent/40 transition-colors text-left w-full"
            >
              {/* Top row: title + score */}
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-text-primary truncate">
                  {result.meeting_title || "Untitled"}
                </span>
                <span className="shrink-0 text-[10px] px-1.5 py-0.5 rounded bg-accent/10 text-accent">
                  {Math.round(result.score * 100)}%
                </span>
              </div>

              {/* Matched text */}
              <p className="text-sm text-text-secondary line-clamp-2">
                {result.text}
              </p>

              {/* Bottom row: speaker + timestamp */}
              <div className="flex items-center gap-2">
                <span className="text-xs text-text-muted">
                  {result.speaker}
                </span>
                <span className="text-xs text-text-muted">
                  {formatTime(result.start_time)}
                </span>
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
