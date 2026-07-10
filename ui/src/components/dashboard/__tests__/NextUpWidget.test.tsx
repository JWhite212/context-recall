import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { NextUpWidget } from "../NextUpWidget";
import { ToastProvider } from "../../common/Toast";
import * as api from "../../../lib/api";
import { useDaemonStatus } from "../../../hooks/useDaemonStatus";

vi.mock("../../../lib/api");
vi.mock("../../../hooks/useDaemonStatus");

const NOW = 1_000_000; // seconds

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter>{children}</MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

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
    const { container } = render(<NextUpWidget />, { wrapper: makeWrapper() });
    // Widget should not render any content (the Shell/div) when daemonRunning is false
    expect(container.querySelector("div.rounded-xl")).not.toBeInTheDocument();
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
});
