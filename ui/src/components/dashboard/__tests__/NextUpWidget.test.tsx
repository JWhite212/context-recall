import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { NextUpWidget } from "../NextUpWidget";
import * as api from "../../../lib/api";
import { useDaemonStatus } from "../../../hooks/useDaemonStatus";
import { makeWrapper } from "../../../test/queryWrapper";

vi.mock("../../../lib/api");
vi.mock("../../../hooks/useDaemonStatus");

const NOW = 1_000_000; // seconds


function ev(
  overrides: Partial<import("../../../lib/types").CalendarEvent> = {},
) {
  return {
    event_uid: "EK1:1000",
    title: "Weekly Sync",
    start_ts: NOW + 720, // 12 min out
    end_ts: NOW + 720 + 1800,
    attendees: [
      { name: "Sam", email: "sam@x.com" },
      { name: "Kim", email: "kim@x.com" },
    ],
    organizer: null,
    join_url: "https://teams.microsoft.com/l/xyz",
    meeting_id: "",
    calendar_name: "Work",
    ...overrides,
  };
}

beforeEach(() => {
  // Deterministic time WITHOUT fake timers: faking timers would stall
  // Testing Library's async findBy*/waitFor (they poll on setTimeout). The
  // widget's 1s interval uses real timers but never fires within a sub-second
  // test, so no act() warnings.
  vi.spyOn(Date, "now").mockReturnValue(NOW * 1000);
  vi.mocked(useDaemonStatus).mockReturnValue({
    daemonRunning: true,
    state: "idle",
    activeMeeting: null,
    isLoading: false,
  } as ReturnType<typeof useDaemonStatus>);
  vi.mocked(api.getPreparedEventUids).mockResolvedValue({ event_uids: [] });
  vi.mocked(api.getCalendarEvents).mockResolvedValue({
    events: [ev()],
    count: 1,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("NextUpWidget", () => {
  it("renders the next upcoming event with a countdown", async () => {
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText("Weekly Sync")).toBeInTheDocument();
    expect(screen.getByText(/in 12 min/)).toBeInTheDocument();
  });

  it("shows 'Happening now' for an in-window event", async () => {
    vi.mocked(api.getCalendarEvents).mockResolvedValue({
      events: [ev({ start_ts: NOW - 240, end_ts: NOW + 1800 })],
      count: 1,
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText(/Happening now/i)).toBeInTheDocument();
  });

  it("shows the empty state when nothing is in the next 24h", async () => {
    vi.mocked(api.getCalendarEvents).mockResolvedValue({
      events: [],
      count: 0,
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText(/Nothing scheduled/i)).toBeInTheDocument();
  });

  it("renders nothing when the daemon is offline", () => {
    vi.mocked(useDaemonStatus).mockReturnValue({
      daemonRunning: false,
      state: "unknown",
      activeMeeting: null,
      isLoading: false,
    } as ReturnType<typeof useDaemonStatus>);
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    // Widget returns null when offline → its "Next up" heading is never rendered.
    // (container isn't empty: the ToastProvider wrapper always renders its
    // notifications div, so toBeEmptyDOMElement would be wrong here.)
    expect(
      screen.queryByRole("heading", { name: "Next up" }),
    ).not.toBeInTheDocument();
  });

  it("shows a 'Prep ready' badge when the event is prepared", async () => {
    vi.mocked(api.getPreparedEventUids).mockResolvedValue({
      event_uids: ["EK1:1000"],
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    expect(await screen.findByText(/Prep ready/i)).toBeInTheDocument();
  });

  it("shows a Join link when the event has a join_url", async () => {
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    const join = await screen.findByRole("link", { name: /join/i });
    expect(join).toHaveAttribute("href", "https://teams.microsoft.com/l/xyz");
  });

  it("opens the prep modal when 'View prep' is clicked (prepared event)", async () => {
    vi.mocked(api.getPreparedEventUids).mockResolvedValue({
      event_uids: ["EK1:1000"],
    });
    vi.mocked(api.getPrepByEvent).mockResolvedValue(null);
    render(<NextUpWidget />, { wrapper: makeWrapper() });

    const viewPrep = await screen.findByRole("button", { name: /view prep/i });
    fireEvent.click(viewPrep);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
  });

  it("fires generatePrepForEvent with the expected body", async () => {
    vi.mocked(api.getPrepByEvent).mockResolvedValue(null);
    vi.mocked(api.generatePrepForEvent).mockResolvedValue({
      id: "p1",
    } as Awaited<ReturnType<typeof api.generatePrepForEvent>>);
    render(<NextUpWidget />, { wrapper: makeWrapper() });

    const gen = await screen.findByRole("button", { name: /generate prep/i });
    fireEvent.click(gen);

    await waitFor(() => {
      expect(api.generatePrepForEvent).toHaveBeenCalledWith(
        expect.objectContaining({
          event_uid: "EK1:1000",
          title: "Weekly Sync",
          attendee_names: ["Sam", "Kim"],
          end_ts: NOW + 720 + 1800,
          series_id: null,
        }),
      );
    });
  });

  it("disables Record when the event is not live", async () => {
    // start_ts 12 min out (> 5 min) → not live yet.
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    const rec = await screen.findByRole("button", {
      name: /record this meeting/i,
    });
    expect(rec).toBeDisabled();
  });

  it("disables Record with 'Already recording' when the daemon is recording", async () => {
    vi.mocked(useDaemonStatus).mockReturnValue({
      daemonRunning: true,
      state: "recording",
      activeMeeting: null,
      isLoading: false,
    } as ReturnType<typeof useDaemonStatus>);
    vi.mocked(api.getCalendarEvents).mockResolvedValue({
      events: [ev({ start_ts: NOW - 60, end_ts: NOW + 1800 })], // live window
      count: 1,
    });
    render(<NextUpWidget />, { wrapper: makeWrapper() });
    const rec = await screen.findByRole("button", {
      name: /record this meeting/i,
    });
    expect(rec).toBeDisabled();
    expect(rec).toHaveAttribute("title", "Already recording");
  });

  it("records via a 2-step confirm when the event is live", async () => {
    vi.mocked(api.getCalendarEvents).mockResolvedValue({
      events: [ev({ start_ts: NOW - 60, end_ts: NOW + 1800 })], // live, not recording
      count: 1,
    });
    vi.mocked(api.startRecording).mockResolvedValue(
      {} as Awaited<ReturnType<typeof api.startRecording>>,
    );
    render(<NextUpWidget />, { wrapper: makeWrapper() });

    fireEvent.click(
      await screen.findByRole("button", { name: /record this meeting/i }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /start recording\?/i }),
    );

    await waitFor(() => {
      expect(api.startRecording).toHaveBeenCalledTimes(1);
    });
  });
});
