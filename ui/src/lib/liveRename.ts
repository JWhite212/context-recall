import type { WSEvent } from "./types";

/**
 * Decide whether a pending live-title override should be applied to the
 * meeting announced by a pipeline.complete event, and with what title.
 *
 * The override is typed in the live view while recording (no meeting row
 * exists yet), so it can only be applied once pipeline.complete arrives
 * with the new meeting_id. But reprocess completions and back-to-back
 * recordings share that event shape — applying blindly could rename the
 * WRONG meeting and stamp it title_source='manual' permanently (C1).
 *
 * The rename is therefore keyed to the recording session: it applies only
 * when the completion is not a reprocess AND its started_at matches the
 * session the user typed the title in, AND the override is non-empty and
 * differs from the calendar-seeded title (a no-op edit).
 *
 * Returns the trimmed title to apply, or null.
 */
export function shouldApplyLiveRename(
  event: WSEvent,
  sessionStartedAt: number | null,
  override: string | null,
  calendarTitle: string | null,
): string | null {
  if (event.type !== "pipeline.complete") return null;
  if (!event.meeting_id) return null;
  if (event.is_reprocess) return null;
  if (event.started_at == null || sessionStartedAt == null) return null;
  if (event.started_at !== sessionStartedAt) return null;
  const pending = override?.trim();
  if (!pending || pending === calendarTitle) return null;
  return pending;
}
