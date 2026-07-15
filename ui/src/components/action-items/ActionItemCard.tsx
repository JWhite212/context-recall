import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { format } from "date-fns";
import { getClients, getProjects, updateActionItem } from "../../lib/api";
import type { ActionItem, ActionItemStatus } from "../../lib/types";

const STATUS_ICONS: Record<ActionItemStatus, string> = {
  open: "○",
  in_progress: "◐",
  done: "●",
  cancelled: "✕",
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
  if (current === "done") return "open";
  return current; // cancelled stays cancelled
}

interface Props {
  item: ActionItem;
  onEdit?: (item: ActionItem) => void;
}

export function ActionItemCard({ item, onEdit }: Props) {
  const queryClient = useQueryClient();
  const [isTagging, setIsTagging] = useState(false);

  const mutation = useMutation({
    mutationFn: (status: ActionItemStatus) =>
      updateActionItem(item.id, { status }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["action-items"] });
    },
    onError: (err: Error) => {
      console.error("Failed to update action item status:", err);
    },
  });

  const { data: clients } = useQuery({
    queryKey: ["clients"],
    queryFn: () => getClients(),
  });
  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: () => getProjects(),
  });

  const tagMutation = useMutation({
    mutationFn: (patch: {
      client_id?: string | null;
      project_id?: string | null;
    }) => updateActionItem(item.id, patch),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["action-items"] });
    },
    onError: (err: Error) => {
      console.error("Failed to update action item tag:", err);
    },
  });

  const clientName = item.client_id
    ? clients?.find((c) => c.id === item.client_id)?.name
    : null;
  const projectName = item.project_id
    ? projects?.find((p) => p.id === item.project_id)?.name
    : null;

  const tagClientId = item.client_id ?? null;
  const taggableProjects = projects?.filter(
    (p) => !tagClientId || p.client_id === tagClientId || p.client_id === null,
  );

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
        aria-label={`Status: ${item.status}. Mark as ${nextStatus(item.status)}`}
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
        <div className="flex items-center gap-2 mt-1 text-xs text-text-muted flex-wrap">
          {item.assignee && <span>{item.assignee}</span>}
          {item.due_date && (
            <span>Due {format(new Date(item.due_date), "MMM d")}</span>
          )}
          <span className={PRIORITY_COLORS[item.priority]}>
            {item.priority}
          </span>
          {clientName && (
            <span className="px-1.5 py-0.5 rounded bg-surface border border-border">
              {clientName}
            </span>
          )}
          {projectName && (
            <span className="px-1.5 py-0.5 rounded bg-surface border border-border">
              {projectName}
            </span>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              setIsTagging((v) => !v);
            }}
            aria-label="Edit client/project tag"
            className="text-text-muted hover:text-text-primary transition-colors underline decoration-dotted"
          >
            Tag
          </button>
        </div>
        {isTagging && (
          <div
            className="flex items-center gap-2 mt-2"
            onClick={(e) => e.stopPropagation()}
          >
            <select
              aria-label="Tag client"
              className="px-2 py-1 text-xs bg-surface border border-border rounded-lg text-text-secondary focus:outline-none focus:ring-2 focus:ring-accent"
              value={item.client_id ?? ""}
              onChange={(e) => {
                const newClientId = e.target.value || null;
                const patch: {
                  client_id: string | null;
                  project_id?: null;
                } = { client_id: newClientId };
                // Don't persist a project that belongs to a different
                // client: clear it in the same PATCH. Client-less
                // projects stay (they are taggable under any client).
                if (newClientId && item.project_id) {
                  const project = projects?.find(
                    (p) => p.id === item.project_id,
                  );
                  const compatible =
                    project != null &&
                    (project.client_id === null ||
                      project.client_id === newClientId);
                  if (!compatible) patch.project_id = null;
                }
                tagMutation.mutate(patch);
              }}
            >
              <option value="">No client</option>
              {clients?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
            <select
              aria-label="Tag project"
              className="px-2 py-1 text-xs bg-surface border border-border rounded-lg text-text-secondary focus:outline-none focus:ring-2 focus:ring-accent"
              value={item.project_id ?? ""}
              onChange={(e) =>
                tagMutation.mutate({ project_id: e.target.value || null })
              }
            >
              <option value="">No project</option>
              {taggableProjects?.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>
    </div>
  );
}
