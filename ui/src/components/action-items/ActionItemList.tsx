import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { getActionItems, getClients, getProjects } from "../../lib/api";
import type {
  ActionItem,
  ActionItemStatus,
  Client,
  Project,
} from "../../lib/types";
import { ActionItemCard } from "./ActionItemCard";
import { ActionItemForm } from "./ActionItemForm";

const FILTERS: { label: string; value: ActionItemStatus | "" }[] = [
  { label: "All", value: "" },
  { label: "Open", value: "open" },
  { label: "In Progress", value: "in_progress" },
  { label: "Done", value: "done" },
];

export type GroupBy =
  "none" | "client" | "project" | "status" | "due" | "meeting";

const GROUP_BY_OPTIONS: { label: string; value: GroupBy }[] = [
  { label: "No grouping", value: "none" },
  { label: "Client", value: "client" },
  { label: "Project", value: "project" },
  { label: "Status", value: "status" },
  { label: "Due date", value: "due" },
  { label: "Meeting", value: "meeting" },
];

const STATUS_LABELS: Record<ActionItemStatus, string> = {
  open: "Open",
  in_progress: "In Progress",
  done: "Done",
  cancelled: "Cancelled",
};

const UNASSIGNED_LABEL = "Unassigned";

type DueBucket = "overdue" | "today" | "this-week" | "later" | "no-date";

const DUE_BUCKET_ORDER: DueBucket[] = [
  "overdue",
  "today",
  "this-week",
  "later",
  "no-date",
];

const DUE_BUCKET_LABELS: Record<DueBucket, string> = {
  overdue: "Overdue",
  today: "Today",
  "this-week": "This Week",
  later: "Later",
  "no-date": "No Due Date",
};

export interface ItemGroup {
  key: string;
  label: string;
  items: ActionItem[];
}

function dueBucketFor(item: ActionItem, now: Date): DueBucket {
  if (!item.due_date) return "no-date";
  const due = new Date(item.due_date);
  if (Number.isNaN(due.getTime())) return "no-date";

  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const startOfTomorrow = new Date(startOfToday);
  startOfTomorrow.setDate(startOfTomorrow.getDate() + 1);
  const endOfWeek = new Date(startOfToday);
  endOfWeek.setDate(endOfWeek.getDate() + 7);

  if (due < startOfToday) return "overdue";
  if (due < startOfTomorrow) return "today";
  if (due < endOfWeek) return "this-week";
  return "later";
}

function groupByKey(
  items: ActionItem[],
  keyFn: (item: ActionItem) => string,
  labelFn: (key: string) => string,
): ItemGroup[] {
  const order: string[] = [];
  const map = new Map<string, ActionItem[]>();
  for (const item of items) {
    const key = keyFn(item);
    if (!map.has(key)) {
      map.set(key, []);
      order.push(key);
    }
    map.get(key)!.push(item);
  }
  return order.map((key) => ({
    key,
    label: labelFn(key),
    items: map.get(key)!,
  }));
}

/**
 * Group `items` client-side by the chosen dimension. `client`/`project`
 * resolve the id to a name via the fetched lookup lists (falling back to
 * "Unassigned" for a null id, or the raw id if the lookup hasn't loaded
 * yet). `due` buckets into overdue/today/this-week/later/no-date in that
 * fixed order. `none` returns everything as one unlabelled group.
 */
export function groupItems(
  items: ActionItem[],
  groupBy: GroupBy,
  lookups: { clients?: Client[]; projects?: Project[] },
): ItemGroup[] {
  if (groupBy === "none") {
    return [{ key: "all", label: "", items }];
  }

  if (groupBy === "status") {
    return groupByKey(
      items,
      (item) => item.status,
      (key) => STATUS_LABELS[key as ActionItemStatus] ?? key,
    );
  }

  if (groupBy === "client") {
    return groupByKey(
      items,
      (item) => item.client_id ?? "",
      (key) => {
        if (!key) return UNASSIGNED_LABEL;
        return (
          lookups.clients?.find((c) => c.id === key)?.name ?? UNASSIGNED_LABEL
        );
      },
    );
  }

  if (groupBy === "project") {
    return groupByKey(
      items,
      (item) => item.project_id ?? "",
      (key) => {
        if (!key) return UNASSIGNED_LABEL;
        return (
          lookups.projects?.find((p) => p.id === key)?.name ?? UNASSIGNED_LABEL
        );
      },
    );
  }

  if (groupBy === "meeting") {
    return groupByKey(
      items,
      (item) => item.meeting_id,
      (key) => key,
    );
  }

  // groupBy === "due"
  const now = new Date();
  const grouped = groupByKey(
    items,
    (item) => dueBucketFor(item, now),
    (key) => DUE_BUCKET_LABELS[key as DueBucket],
  );
  return [...grouped].sort(
    (a, b) =>
      DUE_BUCKET_ORDER.indexOf(a.key as DueBucket) -
      DUE_BUCKET_ORDER.indexOf(b.key as DueBucket),
  );
}

export function ActionItemList() {
  const [statusFilter, setStatusFilter] = useState<ActionItemStatus | "">("");
  const [clientFilter, setClientFilter] = useState("");
  const [projectFilter, setProjectFilter] = useState("");
  const [priorityFilter, setPriorityFilter] = useState("");
  const [groupBy, setGroupBy] = useState<GroupBy>("none");
  const [showForm, setShowForm] = useState(false);
  const [editingItem, setEditingItem] = useState<ActionItem | null>(null);

  const { data, isLoading, isError } = useQuery({
    queryKey: [
      "action-items",
      statusFilter,
      clientFilter,
      projectFilter,
      priorityFilter,
    ],
    queryFn: () =>
      getActionItems({
        status: statusFilter || undefined,
        clientId: clientFilter || undefined,
        projectId: projectFilter || undefined,
        priority: priorityFilter || undefined,
      }),
  });

  const { data: clients } = useQuery({
    queryKey: ["clients"],
    queryFn: () => getClients(),
  });

  const { data: projects } = useQuery({
    queryKey: ["projects"],
    queryFn: () => getProjects(),
  });

  const items = data?.items ?? [];
  const groups = groupItems(items, groupBy, { clients, projects });
  const filterableProjects = projects?.filter(
    (p) =>
      !clientFilter || p.client_id === clientFilter || p.client_id === null,
  );

  const selectClass =
    "px-2 py-1 text-xs bg-surface border border-border rounded-lg text-text-secondary focus:outline-none focus:ring-2 focus:ring-accent";

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
      <div className="flex gap-2 mb-3">
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

      {/* Client / project / priority / group-by controls */}
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <select
          aria-label="Filter by client"
          className={selectClass}
          value={clientFilter}
          onChange={(e) => setClientFilter(e.target.value)}
        >
          <option value="">All clients</option>
          {clients?.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by project"
          className={selectClass}
          value={projectFilter}
          onChange={(e) => setProjectFilter(e.target.value)}
        >
          <option value="">All projects</option>
          {filterableProjects?.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by priority"
          className={selectClass}
          value={priorityFilter}
          onChange={(e) => setPriorityFilter(e.target.value)}
        >
          <option value="">All priorities</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="urgent">Urgent</option>
        </select>
        <select
          aria-label="Group by"
          className={`${selectClass} ml-auto`}
          value={groupBy}
          onChange={(e) => setGroupBy(e.target.value as GroupBy)}
        >
          {GROUP_BY_OPTIONS.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
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
        <div className="space-y-4">
          {groups.map((group) => (
            <div key={group.key}>
              {groupBy !== "none" && (
                <h3 className="text-xs font-semibold uppercase tracking-wide text-text-muted mb-2">
                  {group.label}
                </h3>
              )}
              <div className="space-y-2">
                {group.items.map((item) => (
                  <ActionItemCard
                    key={item.id}
                    item={item}
                    onEdit={setEditingItem}
                  />
                ))}
              </div>
            </div>
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
