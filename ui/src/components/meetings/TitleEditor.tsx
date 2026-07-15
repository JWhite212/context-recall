import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { renameMeeting } from "../../lib/api";
import { useToast } from "../common/Toast";

/**
 * Inline click-to-edit meeting title. Enter/blur commits via renameMeeting
 * (setting title_source='manual' server-side) and invalidates the meeting
 * queries; Escape cancels; an empty or unchanged value is a no-op.
 */
export function TitleEditor({
  meetingId,
  title,
  onRenamed,
  className,
}: {
  meetingId: string;
  title: string;
  onRenamed?: (title: string) => void;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);

  const rename = useMutation({
    mutationFn: (next: string) => renameMeeting(meetingId, next),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      onRenamed?.(data.title);
      setEditing(false);
    },
    onError: () => {
      toast.error("Failed to rename meeting.");
      setEditing(false);
    },
  });

  function commit() {
    const next = value.trim();
    if (!next || next === title) {
      setValue(title);
      setEditing(false);
      return;
    }
    rename.mutate(next);
  }

  if (!editing) {
    return (
      <button
        type="button"
        title="Click to rename"
        onClick={() => {
          setValue(title);
          setEditing(true);
        }}
        className={className ?? "text-left"}
      >
        {title}
      </button>
    );
  }

  return (
    <input
      autoFocus
      value={value}
      disabled={rename.isPending}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setValue(title);
          setEditing(false);
        }
      }}
      className={className ?? "bg-surface border border-border rounded px-1"}
    />
  );
}
