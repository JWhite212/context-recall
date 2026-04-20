import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getActionItems } from "../../lib/api";
import type { ActionItem, ActionItemStatus } from "../../lib/types";
import { ActionItemCard } from "./ActionItemCard";
import { ActionItemForm } from "./ActionItemForm";

const FILTERS: { label: string; value: ActionItemStatus | "" }[] = [
  { label: "All", value: "" },
  { label: "Open", value: "open" },
  { label: "In Progress", value: "in_progress" },
  { label: "Done", value: "done" },
];

export function ActionItemList() {
  const [statusFilter, setStatusFilter] = useState<ActionItemStatus | "">("");
  const [showForm, setShowForm] = useState(false);
  const [editingItem, setEditingItem] = useState<ActionItem | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: ["action-items", statusFilter],
    queryFn: () => getActionItems(statusFilter || undefined),
  });

  const items = data?.items ?? [];

  return (
    <div className="p-6 max-w-3xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-text-primary">Action Items</h1>
        <button
          onClick={() => setShowForm(true)}
          className="px-3 py-1.5 text-sm bg-accent text-white rounded-lg hover:opacity-90 transition-opacity"
        >
          + New
        </button>
      </div>

      {/* Status filters */}
      <div className="flex gap-2 mb-4">
        {FILTERS.map((f) => (
          <button
            key={f.value}
            onClick={() => setStatusFilter(f.value)}
            className={`px-3 py-1 text-xs rounded-full transition-colors ${
              statusFilter === f.value
                ? "bg-accent text-white"
                : "bg-surface border border-border text-text-secondary hover:text-text-primary"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Content */}
      {isError ? (
        <p className="text-sm text-status-error text-center py-12">
          Failed to load action items. Please try again.
        </p>
      ) : isLoading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-16 rounded-lg bg-surface border border-border animate-pulse"
            />
          ))}
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm text-text-muted text-center py-12">
          No action items found.
        </p>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <ActionItemCard key={item.id} item={item} onEdit={setEditingItem} />
          ))}
        </div>
      )}

      {/* Create modal */}
      {showForm && <ActionItemForm onClose={() => setShowForm(false)} />}

      {/* Edit modal */}
      {editingItem && (
        <ActionItemForm
          item={editingItem}
          onClose={() => setEditingItem(null)}
        />
      )}
    </div>
  );
}
