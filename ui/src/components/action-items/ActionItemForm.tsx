import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { createActionItem, updateActionItem } from "../../lib/api";
import type { ActionItem, ActionItemPriority } from "../../lib/types";

interface Props {
  item?: ActionItem | null;
  defaultMeetingId?: string;
  onClose: () => void;
}

export function ActionItemForm({ item, defaultMeetingId, onClose }: Props) {
  const isEditing = !!item;
  const queryClient = useQueryClient();

  const [title, setTitle] = useState(item?.title ?? "");
  const [assignee, setAssignee] = useState(item?.assignee ?? "");
  const [priority, setPriority] = useState(item?.priority ?? "medium");
  const [dueDate, setDueDate] = useState(item?.due_date ?? "");
  const [description, setDescription] = useState(item?.description ?? "");

  const createMutation = useMutation({
    mutationFn: createActionItem,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["action-items"] });
      onClose();
    },
  });

  const updateMutation = useMutation({
    mutationFn: (data: Partial<ActionItem>) => updateActionItem(item!.id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["action-items"] });
      onClose();
    },
  });

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;

    if (isEditing) {
      updateMutation.mutate({
        title: title.trim(),
        assignee: assignee.trim() || null,
        priority: priority as ActionItem["priority"],
        due_date: dueDate || null,
        description: description.trim() || null,
      });
    } else {
      createMutation.mutate({
        meeting_id: defaultMeetingId ?? "",
        title: title.trim(),
        assignee: assignee.trim() || undefined,
        priority,
        due_date: dueDate || undefined,
        description: description.trim() || undefined,
      });
    }
  }

  const inputClass =
    "w-full px-3 py-2 bg-surface border border-border rounded-lg text-sm text-text-primary placeholder:text-text-muted focus:outline-none focus:ring-2 focus:ring-accent";

  const isBusy = createMutation.isPending || updateMutation.isPending;

  return (
    <div
      className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="bg-surface-raised border border-border rounded-xl p-6 w-full max-w-md shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold text-text-primary mb-4">
          {isEditing ? "Edit Action Item" : "New Action Item"}
        </h2>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-xs text-text-muted mb-1">Title</label>
            <input
              className={inputClass}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="What needs to be done?"
              autoFocus
            />
          </div>
          <div>
            <label className="block text-xs text-text-muted mb-1">
              Assignee
            </label>
            <input
              className={inputClass}
              value={assignee}
              onChange={(e) => setAssignee(e.target.value)}
              placeholder="Who is responsible?"
            />
          </div>
          <div className="flex gap-3">
            <div className="flex-1">
              <label className="block text-xs text-text-muted mb-1">
                Priority
              </label>
              <select
                className={inputClass}
                value={priority}
                onChange={(e) =>
                  setPriority(e.target.value as ActionItemPriority)
                }
              >
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </select>
            </div>
            <div className="flex-1">
              <label className="block text-xs text-text-muted mb-1">
                Due date
              </label>
              <input
                type="date"
                className={inputClass}
                value={dueDate}
                onChange={(e) => setDueDate(e.target.value)}
              />
            </div>
          </div>
          <div>
            <label className="block text-xs text-text-muted mb-1">
              Description
            </label>
            <textarea
              className={`${inputClass} resize-none`}
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Additional details..."
            />
          </div>
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 text-sm text-text-secondary hover:text-text-primary transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!title.trim() || isBusy}
              className="px-4 py-2 text-sm bg-accent text-white rounded-lg hover:opacity-90 transition-opacity disabled:opacity-50"
            >
              {isEditing ? "Save" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
