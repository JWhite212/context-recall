import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { getActionItems } from "../../lib/api";
import type { ActionItem } from "../../lib/types";

function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

export function OverdueItems() {
  const navigate = useNavigate();

  const { data, isLoading, isError } = useQuery({
    queryKey: ["action-items", "open"],
    queryFn: () => getActionItems("open"),
  });

  if (isLoading || isError || !data) return null;

  const now = Date.now();
  const overdueItems = data.items
    .filter((item): item is ActionItem & { due_date: string } => {
      if (!item.due_date) return false;
      const dueAt = new Date(item.due_date).getTime();
      return Number.isFinite(dueAt) && dueAt < now;
    })
    .sort(
      (a, b) => new Date(a.due_date).getTime() - new Date(b.due_date).getTime(),
    );

  if (overdueItems.length === 0) return null;

  return (
    <div className="rounded-xl bg-surface-raised border border-border p-6">
      <div className="flex items-center gap-2 mb-4">
        <h2 className="text-sm font-medium text-text-primary">Overdue Items</h2>
        <span className="text-xs px-2 py-0.5 rounded-full bg-status-error/20 text-status-error">
          {overdueItems.length}
        </span>
      </div>
      <div className="flex flex-col gap-1">
        {overdueItems.slice(0, 5).map((item) => (
          <button
            key={item.id}
            onClick={() => navigate("/action-items")}
            className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-sidebar-hover transition-colors cursor-pointer text-left w-full"
          >
            <div className="min-w-0">
              <p className="text-sm text-text-primary truncate">{item.title}</p>
              <p className="text-xs text-text-muted">
                {item.assignee && `${item.assignee} \u00B7 `}
                Due {formatDate(item.due_date)}
              </p>
            </div>
          </button>
        ))}
      </div>
      {overdueItems.length > 5 && (
        <button
          onClick={() => navigate("/action-items")}
          className="text-xs text-text-muted hover:text-text-primary transition-colors mt-2 px-3"
        >
          View all {overdueItems.length} overdue items
        </button>
      )}
    </div>
  );
}
