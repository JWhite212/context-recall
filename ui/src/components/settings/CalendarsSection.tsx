import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  getCalendars,
  getConfig,
  updateConfig,
  triggerCalendarSync,
} from "../../lib/api";
import { useToast } from "../common/Toast";

/** Settings panel: choose which calendars to import, and sync now. */
export function CalendarsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: calData } = useQuery({
    queryKey: ["calendars"],
    queryFn: getCalendars,
  });
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });

  const excluded = config?.calendar?.excluded_calendars ?? [];
  const calendars = calData?.calendars ?? [];

  const save = useMutation({
    mutationFn: (next: string[]) =>
      updateConfig({ calendar: { excluded_calendars: next } } as never),
    onSuccess: (data) => {
      queryClient.setQueryData(["config"], data);
      toast.success("Calendar selection saved.");
    },
    onError: () => toast.error("Failed to save calendar selection."),
  });

  const syncNow = useMutation({
    mutationFn: triggerCalendarSync,
    onSuccess: (r) => toast.success(`Synced ${r.synced} events.`),
    onError: () => toast.error("Sync failed."),
  });

  function toggle(title: string, include: boolean) {
    const next = include
      ? excluded.filter((t) => t !== title)
      : [...excluded, title];
    save.mutate(next);
  }

  return (
    <fieldset
      id={id}
      className="scroll-mt-20 rounded-xl bg-surface-raised border border-border p-5"
    >
      <legend className="sr-only">Calendars</legend>
      <h2 className="text-sm font-medium text-text-primary">Calendars</h2>
      <p className="text-xs text-text-muted mt-1">
        Choose which calendars to import upcoming meetings from.
      </p>

      <div className="py-3 flex flex-col gap-2">
        {calendars.length === 0 ? (
          <p className="text-sm text-text-muted">No calendars available.</p>
        ) : (
          calendars.map((c) => {
            const included = !excluded.includes(c.title);
            return (
              <label
                key={c.id}
                className="flex items-center gap-2 text-sm text-text-secondary"
              >
                <input
                  type="checkbox"
                  checked={included}
                  onChange={(e) => toggle(c.title, e.target.checked)}
                />
                {c.title}
              </label>
            );
          })
        )}
      </div>

      <button
        type="button"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending}
        className="self-end px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
      >
        Sync now
      </button>
    </fieldset>
  );
}
