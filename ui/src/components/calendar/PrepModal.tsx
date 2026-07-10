import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import Markdown from "react-markdown";
import { getPrepByEvent } from "../../lib/api";

interface PrepModalProps {
  eventUid: string;
  title: string;
  onClose: () => void;
}

export function PrepModal({ eventUid, title, onClose }: PrepModalProps) {
  const {
    data: briefing,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["prep", "by-event", eventUid],
    queryFn: () => getPrepByEvent(eventUid),
  });
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    panelRef.current?.focus();
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previouslyFocused?.focus?.();
    };
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="prep-modal-title"
        className="w-full max-w-2xl max-h-[80vh] overflow-y-auto rounded-xl border border-border bg-surface-raised p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
          <h2
            id="prep-modal-title"
            className="text-sm font-semibold text-text-primary"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-text-muted hover:text-text-primary"
          >
            ✕
          </button>
        </div>
        {isLoading ? (
          <div className="space-y-2">
            <div className="h-4 w-5/6 bg-surface border border-border rounded animate-pulse" />
            <div className="h-4 w-2/3 bg-surface border border-border rounded animate-pulse" />
          </div>
        ) : isError ? (
          <p className="text-sm text-text-muted">Failed to load briefing.</p>
        ) : briefing ? (
          <div className="prose prose-sm prose-invert max-w-none [&_h1]:text-base [&_h2]:text-sm [&_h2]:mt-4 [&_li]:text-text-secondary [&_p]:text-text-secondary">
            <Markdown>{briefing.content_markdown}</Markdown>
          </div>
        ) : (
          <p className="text-sm text-text-muted">No briefing available.</p>
        )}
      </div>
    </div>
  );
}
