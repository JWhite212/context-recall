import { describe, it, expect } from "vitest";
import { shouldApplyLiveRename } from "../liveRename";
import type { WSEvent } from "../types";

/** The M6 test: session-keyed application of the pending live title.
 *  A pending liveTitleOverride must only ever be applied to the meeting
 *  produced by the SAME live session the user typed it in — never to a
 *  reprocess completion or a back-to-back recording (C1). */

const SESSION = 1000;

function complete(
  overrides: Partial<Extract<WSEvent, { type: "pipeline.complete" }>> = {},
): WSEvent {
  return {
    type: "pipeline.complete",
    meeting_id: "m1",
    title: "Auto Title",
    is_reprocess: false,
    started_at: SESSION,
    ...overrides,
  };
}

describe("shouldApplyLiveRename", () => {
  it("applies the pending override for the matching live session", () => {
    expect(shouldApplyLiveRename(complete(), SESSION, "My Title", null)).toBe(
      "My Title",
    );
  });

  it("trims the override before applying", () => {
    expect(
      shouldApplyLiveRename(complete(), SESSION, "  My Title  ", null),
    ).toBe("My Title");
  });

  it("does NOT apply on a reprocess completion (C1)", () => {
    expect(
      shouldApplyLiveRename(
        complete({ is_reprocess: true }),
        SESSION,
        "My Title",
        null,
      ),
    ).toBeNull();
  });

  it("does NOT apply when started_at differs from the live session (C1 back-to-back)", () => {
    expect(
      shouldApplyLiveRename(
        complete({ started_at: SESSION + 60 }),
        SESSION,
        "My Title",
        null,
      ),
    ).toBeNull();
  });

  it("does NOT apply when the event carries no started_at (old daemon)", () => {
    expect(
      shouldApplyLiveRename(
        complete({ started_at: undefined }),
        SESSION,
        "My Title",
        null,
      ),
    ).toBeNull();
  });

  it("does NOT apply when no live session is tracked", () => {
    expect(
      shouldApplyLiveRename(complete(), null, "My Title", null),
    ).toBeNull();
  });

  it("does NOT apply without a meeting_id", () => {
    expect(
      shouldApplyLiveRename(
        complete({ meeting_id: null }),
        SESSION,
        "My Title",
        null,
      ),
    ).toBeNull();
  });

  it("does NOT apply an empty or whitespace-only override", () => {
    expect(shouldApplyLiveRename(complete(), SESSION, null, null)).toBeNull();
    expect(shouldApplyLiveRename(complete(), SESSION, "   ", null)).toBeNull();
  });

  it("does NOT apply when the override equals the calendar title (no-op edit)", () => {
    expect(
      shouldApplyLiveRename(complete(), SESSION, "Weekly Sync", "Weekly Sync"),
    ).toBeNull();
  });

  it("ignores non-completion events", () => {
    expect(
      shouldApplyLiveRename(
        { type: "meeting.started", started_at: SESSION },
        SESSION,
        "My Title",
        null,
      ),
    ).toBeNull();
  });
});
