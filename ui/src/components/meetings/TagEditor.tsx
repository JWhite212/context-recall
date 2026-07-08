import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getMeetingTags, setMeetingTags } from "../../lib/api";
import { useToast } from "../common/Toast";

/**
 * Editable multi-tag chip control for a meeting. Every tag (including
 * auto-generated summary tags and folded labels) is a removable chip; new
 * tags are added explicitly via Enter or the Add button — never on blur.
 */
export function TagEditor({
  meetingId,
  tags,
}: {
  meetingId: string;
  tags: string[];
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [draft, setDraft] = useState("");
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const { data: allTags = [] } = useQuery({
    queryKey: ["meeting-tags"],
    queryFn: getMeetingTags,
    staleTime: 30_000,
  });

  const saveTags = useMutation({
    mutationFn: (next: string[]) => setMeetingTags(meetingId, next),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      queryClient.invalidateQueries({ queryKey: ["meeting-tags"] });
    },
    onError: () => {
      toast.error("Failed to save tags.");
    },
  });

  const addTag = (raw: string) => {
    // A save in flight means `tags` is stale (the parent refetches on
    // success), so block until it settles or we could submit a stale array
    // and clobber a concurrent edit.
    if (saveTags.isPending) return;
    const tag = raw.trim();
    setDraft("");
    setOpen(false);
    if (!tag || tags.includes(tag)) return;
    saveTags.mutate([...tags, tag]);
  };

  const removeTag = (tag: string) => {
    if (saveTags.isPending) return;
    saveTags.mutate(tags.filter((t) => t !== tag));
  };

  const suggestions = allTags.filter(
    (t) => !tags.includes(t) && t.toLowerCase().includes(draft.toLowerCase()),
  );

  useEffect(() => {
    if (!open) return;
    const handleClick = (e: MouseEvent) => {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <div
      className="relative inline-flex flex-wrap items-center gap-1.5"
      ref={wrapperRef}
    >
      {tags.map((tag) => (
        <span
          key={tag}
          className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full bg-accent/10 text-accent"
        >
          {tag}
          <button
            onClick={() => removeTag(tag)}
            disabled={saveTags.isPending}
            aria-label={`Remove tag ${tag}`}
            className="opacity-60 hover:opacity-100 transition-opacity disabled:opacity-30"
          >
            <svg
              width="10"
              height="10"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </span>
      ))}
      <input
        type="text"
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            addTag(draft);
          }
          if (e.key === "Escape") {
            setDraft("");
            setOpen(false);
            (e.target as HTMLInputElement).blur();
          }
        }}
        placeholder="Add tag..."
        aria-label="Add meeting tag"
        className="px-2 py-1 text-xs rounded-md bg-surface-raised border border-border text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-1 focus:ring-accent w-28"
      />
      <button
        onClick={() => addTag(draft)}
        disabled={!draft.trim() || saveTags.isPending}
        className="text-xs px-2 py-1 rounded-md bg-accent/10 text-accent hover:bg-accent/20 disabled:opacity-40 transition-colors"
      >
        Add
      </button>
      {open && suggestions.length > 0 && (
        <div className="absolute left-0 top-full mt-1 w-40 rounded-lg bg-surface-raised border border-border shadow-lg z-10 py-1 max-h-32 overflow-y-auto">
          {suggestions.map((tag) => (
            <button
              key={tag}
              onMouseDown={(e) => {
                e.preventDefault();
                addTag(tag);
              }}
              className="w-full text-left px-3 py-1.5 text-xs text-text-secondary hover:bg-sidebar-hover transition-colors"
            >
              {tag}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
