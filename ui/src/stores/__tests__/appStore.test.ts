import { describe, it, expect, beforeEach } from "vitest";
import { useAppStore } from "../appStore";

function resetStore() {
  useAppStore.setState({
    pipelineStage: null,
    pipelineWarning: null,
    warnings: [],
    lastPipelineError: null,
    liveSegments: [],
    audioLevels: { system: 0, mic: 0 },
  });
}

describe("appStore — pipeline.warning handling (Bug A1 UI surface)", () => {
  beforeEach(resetStore);

  it("starts with no pipeline warning", () => {
    expect(useAppStore.getState().pipelineWarning).toBeNull();
  });

  it("stores a pipeline.warning event so the UI can render the hint", () => {
    // The daemon emits this when the system audio source has been silent
    // for ~10s — typically BlackHole installed but not routed via Audio
    // MIDI Setup. The user needs the actionable hint while the meeting
    // is still in progress.
    useAppStore.getState().handleEvent({
      type: "pipeline.warning",
      source: "system",
      message: "No system audio detected. Check Multi-Output Device routing.",
    });

    const warning = useAppStore.getState().pipelineWarning;
    expect(warning).not.toBeNull();
    expect(warning?.source).toBe("system");
    expect(warning?.message).toMatch(/system audio/i);
  });

  it("clears the warning on pipeline.complete (recording succeeded)", () => {
    useAppStore.setState({
      pipelineWarning: { source: "system", message: "stale" },
    });

    useAppStore.getState().handleEvent({
      type: "pipeline.complete",
      meeting_id: "m1",
      title: "ok",
    });

    expect(useAppStore.getState().pipelineWarning).toBeNull();
  });

  it("clears the warning on pipeline.error (recording failed)", () => {
    useAppStore.setState({
      pipelineWarning: { source: "system", message: "stale" },
    });

    useAppStore.getState().handleEvent({
      type: "pipeline.error",
      meeting_id: "m1",
      stage: "transcribing",
      error: "boom",
    });

    expect(useAppStore.getState().pipelineWarning).toBeNull();
  });

  it("clears the warning when a new meeting starts (fresh session)", () => {
    useAppStore.setState({
      pipelineWarning: { source: "system", message: "stale from last session" },
    });

    useAppStore.getState().handleEvent({
      type: "meeting.started",
      started_at: 1234567890,
    });

    expect(useAppStore.getState().pipelineWarning).toBeNull();
  });

  it("resetLive() clears the warning along with other live state", () => {
    useAppStore.setState({
      pipelineWarning: { source: "system", message: "stale" },
      pipelineStage: "transcribing",
    });

    useAppStore.getState().resetLive();

    const state = useAppStore.getState();
    expect(state.pipelineWarning).toBeNull();
    expect(state.pipelineStage).toBeNull();
  });
});

describe("appStore — warnings slice (Unit 14 diagnostics banner)", () => {
  beforeEach(resetStore);

  it("pushWarning appends a warning and dismissWarning removes it by id", () => {
    useAppStore.getState().pushWarning({
      id: "a",
      source: "system",
      message: "silent",
      createdAt: 1,
    });
    expect(useAppStore.getState().warnings).toHaveLength(1);

    useAppStore.getState().dismissWarning("a");
    expect(useAppStore.getState().warnings).toHaveLength(0);
  });

  it("populates warnings from pipeline.warning events and de-dupes by source+message", () => {
    useAppStore.getState().handleEvent({
      type: "pipeline.warning",
      source: "system",
      message: "BlackHole silent",
    });
    useAppStore.getState().handleEvent({
      type: "pipeline.warning",
      source: "system",
      message: "BlackHole silent",
    });

    expect(useAppStore.getState().warnings).toHaveLength(1);
    expect(useAppStore.getState().warnings[0].source).toBe("system");
  });

  it("clears warnings on pipeline.complete and records error on pipeline.error", () => {
    useAppStore
      .getState()
      .pushWarning({ id: "x", source: "mic", message: "perm", createdAt: 1 });

    useAppStore.getState().handleEvent({
      type: "pipeline.error",
      meeting_id: "m1",
      stage: "transcribing",
      error: "microphone not available",
    });

    const state = useAppStore.getState();
    expect(state.warnings).toHaveLength(0);
    expect(state.lastPipelineError).toBe("microphone not available");

    useAppStore.getState().handleEvent({
      type: "pipeline.complete",
      meeting_id: "m1",
      title: "ok",
    });
    expect(useAppStore.getState().lastPipelineError).toBeNull();
  });

  it("clears warnings when a new meeting starts", () => {
    useAppStore.getState().pushWarning({
      id: "stale",
      source: "system",
      message: "old",
      createdAt: 1,
    });

    useAppStore.getState().handleEvent({
      type: "meeting.started",
      started_at: 1234567890,
    });

    expect(useAppStore.getState().warnings).toHaveLength(0);
  });
});

describe("appStore — live calendar title (Feature 2 rename)", () => {
  beforeEach(() => {
    resetStore();
    useAppStore.setState({ liveCalendarTitle: null, liveTitleOverride: null });
  });

  it("seeds liveCalendarTitle from a meeting.calendar_match event", () => {
    useAppStore.getState().handleEvent({
      type: "meeting.calendar_match",
      title: "Weekly Sync",
      attendees: [],
      confidence: 0.9,
    });
    expect(useAppStore.getState().liveCalendarTitle).toBe("Weekly Sync");
  });

  it("clears liveCalendarTitle on pipeline.complete", () => {
    useAppStore.setState({ liveCalendarTitle: "Weekly Sync" });
    useAppStore.getState().handleEvent({
      type: "pipeline.complete",
      meeting_id: "m1",
      title: "Weekly Sync",
    });
    expect(useAppStore.getState().liveCalendarTitle).toBeNull();
  });

  it("clears liveCalendarTitle on resetLive", () => {
    useAppStore.setState({ liveCalendarTitle: "Weekly Sync" });
    useAppStore.getState().resetLive();
    expect(useAppStore.getState().liveCalendarTitle).toBeNull();
  });

  it("setLiveTitleOverride stores the user's live edit", () => {
    useAppStore.getState().setLiveTitleOverride("My Notes");
    expect(useAppStore.getState().liveTitleOverride).toBe("My Notes");
  });

  it("clears liveTitleOverride on pipeline.complete and resetLive", () => {
    useAppStore.setState({ liveTitleOverride: "My Notes" });
    useAppStore.getState().handleEvent({
      type: "pipeline.complete",
      meeting_id: "m1",
      title: "x",
    });
    expect(useAppStore.getState().liveTitleOverride).toBeNull();

    useAppStore.setState({ liveTitleOverride: "Again" });
    useAppStore.getState().resetLive();
    expect(useAppStore.getState().liveTitleOverride).toBeNull();
  });
});

describe("appStore — live session keying (C1/I1)", () => {
  beforeEach(() => {
    resetStore();
    useAppStore.setState({
      liveCalendarTitle: null,
      liveTitleOverride: null,
      liveSessionStartedAt: null,
    });
  });

  it("records the session key on meeting.started", () => {
    useAppStore.getState().handleEvent({
      type: "meeting.started",
      started_at: 1234.5,
    });
    expect(useAppStore.getState().liveSessionStartedAt).toBe(1234.5);
  });

  it("meeting.started clears stale live titles from a previous session (I1)", () => {
    // Leftovers from a session that errored or never completed must not
    // leak into the next recording (the daemon emits meeting.started
    // BEFORE meeting.calendar_match, so the fresh seed still lands).
    useAppStore.setState({
      liveCalendarTitle: "Old Sync",
      liveTitleOverride: "Old Override",
    });
    useAppStore.getState().handleEvent({
      type: "meeting.started",
      started_at: 999,
    });
    const s = useAppStore.getState();
    expect(s.liveCalendarTitle).toBeNull();
    expect(s.liveTitleOverride).toBeNull();
  });

  it("pipeline.error clears live titles and the session key (I1)", () => {
    useAppStore.setState({
      liveCalendarTitle: "Sync",
      liveTitleOverride: "Typed",
      liveSessionStartedAt: 42,
    });
    useAppStore.getState().handleEvent({
      type: "pipeline.error",
      meeting_id: "m1",
      stage: "transcribing",
      error: "boom",
    });
    const s = useAppStore.getState();
    expect(s.liveCalendarTitle).toBeNull();
    expect(s.liveTitleOverride).toBeNull();
    expect(s.liveSessionStartedAt).toBeNull();
  });

  it("pipeline.complete and resetLive clear the session key", () => {
    useAppStore.setState({ liveSessionStartedAt: 42 });
    useAppStore.getState().handleEvent({
      type: "pipeline.complete",
      meeting_id: "m1",
      title: "x",
    });
    expect(useAppStore.getState().liveSessionStartedAt).toBeNull();

    useAppStore.setState({ liveSessionStartedAt: 43 });
    useAppStore.getState().resetLive();
    expect(useAppStore.getState().liveSessionStartedAt).toBeNull();
  });
});
