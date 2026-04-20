import { useMutation, useQueryClient } from "@tanstack/react-query";
import { format } from "date-fns";
import { updateActionItem } from "../../lib/api";
import type { ActionItem, ActionItemStatus } from "../../lib/types";

const STATUS_ICONS: Record<ActionItemStatus, string> = {
  open: "\u25CB",
  in_progress: "\u25D0",
  done: "\u25CF",
  cancelled: "\u2715",
};

const PRIORITY_COLORS: Record<string, string> = {
  urgent: "text-status-error",
  high: "text-orange-400",
  medium: "text-text-secondary",
  low: "text-text-muted",
};

function nextStatus(current: ActionItemStatus): ActionItemStatus {
  if (current === "open") return "in_progress";
  if (current === "in_progress") return "done";
  return "open";
}

interface Props {
  item: ActionItem;
  onEdit?: (item: ActionItem) => void;
}

export function ActionItemCard({ item, onEdit }: Props) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (status: ActionItemStatus) =>
      updateActionItem(item.id, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["action-items"] });
    },
  });

  return (
    <div
      className="flex items-start gap-3 p-3 rounded-lg border border-border hover:bg-surface-raised transition-colors cursor-pointer"
      onClick={() => onEdit?.(item)}
    >
      <button
        onClick={(e) => {
          e.stopPropagation();
          mutation.mutate(nextStatus(item.status));
        }}
        className="mt-0.5 text-lg leading-none hover:opacity-70 transition-opacity"
        aria-label={`Mark as ${nextStatus(item.status)}`}
      >
        {STATUS_ICONS[item.status]}
      </button>
      <div className="flex-1 min-w-0">
        <p
          className={`text-sm font-medium ${
            item.status === "done"
              ? "line-through text-text-muted"
              : "text-text-primary"
          }`}
        >
          {item.title}
        </p>
        <div className="flex items-center gap-2 mt-1 text-xs text-text-muted">
          {item.assignee && <span>{item.assignee}</span>}
          {item.due_date && (
            <span>Due {format(new Date(item.due_date), "MMM d")}</span>
          )}
          <span className={PRIORITY_COLORS[item.priority]}>
            {item.priority}
          </span>
        </div>
      </div>
    </div>
  );
}
