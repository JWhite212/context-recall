import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  getCalendars,
  getCalendarPermission,
  getConfig,
  updateConfig,
  triggerCalendarSync,
} from "../../lib/api";
import { useToast } from "../common/Toast";

/** Settings panel: choose which calendars to import, and sync now. */
export function CalendarsSection({ id }: { id?: string }) {
  const queryClient = useQueryClient();
  const toast = useToast();

  const { data: permission } = useQuery({
    queryKey: ["calendar-permission"],
    queryFn: getCalendarPermission,
  });
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
  const granted = permission?.granted ?? true;

  // Titles that appear on more than one calendar are ambiguous, so show the
  // account (source) alongside them to distinguish, e.g. "Calendar" on iCloud
  // vs. on Google.
  const titleCounts = new Map<string, number>();
  for (const c of calendars) {
    titleCounts.set(c.title, (titleCounts.get(c.title) ?? 0) + 1);
  }
  const duplicateTitles = new Set(
    [...titleCounts].filter(([, n]) => n > 1).map(([t]) => t),
  );

  const save = useMutation({
    mutationFn: (next: string[]) =>
      updateConfig({ calendar: { excluded_calendars: next } }),
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

  // Exclusion is keyed by calendar id so two distinct calendars sharing a
  // title (e.g. "Calendar" in iCloud and in Google) toggle independently. A
  // legacy title entry (pre-id configs) is still treated as excluded and is
  // dropped when the calendar is re-included, migrating it to id-based.
  function isExcluded(cal: { id: string; title: string }) {
    return excluded.includes(cal.id) || excluded.includes(cal.title);
  }

  function toggle(cal: { id: string; title: string }, include: boolean) {
    const cleaned = excluded.filter((x) => x !== cal.id && x !== cal.title);
    const next = include ? cleaned : [...cleaned, cal.id];
    save.mutate(next);
  }

  async function openSystemSettings() {
    // Route through the Tauri opener plugin: a plain window.open() of a
    // custom x-apple.systempreferences: scheme is intercepted by the
    // WKWebview and never reaches macOS, so the button appears to do
    // nothing. openUrl() hands the URL to the OS handler.
    try {
      await openUrl(
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Calendars",
      );
    } catch {
      toast.error("Could not open System Settings.");
    }
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

      {!granted && (
        <div className="mt-3 rounded-lg border border-border bg-surface p-3">
          <p className="text-sm text-text-secondary">
            Calendar access is not granted. Context Recall needs macOS Calendar
            permission to import your meetings.
          </p>
          <button
            type="button"
            onClick={openSystemSettings}
            className="mt-2 px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
          >
            Open System Settings
          </button>
        </div>
      )}

      {granted && (
        <div className="py-3 flex flex-col gap-2">
          {calendars.length === 0 ? (
            <p className="text-sm text-text-muted">No calendars available.</p>
          ) : (
            calendars.map((c) => {
              const included = !isExcluded(c);
              const label =
                duplicateTitles.has(c.title) && c.source
                  ? `${c.title} — ${c.source}`
                  : c.title;
              return (
                <label
                  key={c.id}
                  className="flex items-center gap-2 text-sm text-text-secondary"
                >
                  <input
                    type="checkbox"
                    checked={included}
                    onChange={(e) => toggle(c, e.target.checked)}
                  />
                  {label}
                </label>
              );
            })
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => syncNow.mutate()}
        disabled={syncNow.isPending || !granted}
        className="self-end px-3 py-1.5 text-xs rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
      >
        Sync now
      </button>
    </fieldset>
  );
}
