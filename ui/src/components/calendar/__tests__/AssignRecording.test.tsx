import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";

vi.mock("../../../hooks/useDaemonStatus", () => ({
  useDaemonStatus: () => ({
    state: "idle",
    daemonRunning: true,
    activeMeeting: null,
  }),
}));

const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Quick Catch-Up",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "Work",
};

describe("UpcomingEventCard assign-a-recording", () => {
  const calls: { url: string; method?: string }[] = [];
  beforeEach(() => {
    calls.length = 0;
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = input.toString();
        calls.push({ url, method: init?.method });
        if (url.includes("/api/calendar/meetings")) {
          return new Response(
            JSON.stringify({
              meetings: [
                {
                  id: "rec1",
                  title: "Amelia Check-In",
                  started_at: 1_699_999_000,
                  ended_at: 1_700_001_000,
                  duration_seconds: 1380,
                  status: "complete",
                  tags: [],
                  calendar_event_title: "",
                  attendees_json: "[]",
                  calendar_confidence: 0,
                  teams_join_url: "",
                  teams_meeting_id: "",
                  calendar_event_uid: "",
                },
              ],
              count: 1,
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ id: "rec1", calendar_event_uid: "EK1:1000" }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      },
    ) as unknown as typeof fetch;
  });

  it("links a chosen recording to the event", async () => {
    render(<UpcomingEventCard event={EVENT} />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Quick Catch-Up/i }));
    fireEvent.click(screen.getByText(/Assign a recording/i));
    await waitFor(() =>
      expect(screen.getByText("Amelia Check-In")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByText("Amelia Check-In"));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/meetings/rec1/calendar-link") &&
            c.method === "PUT",
        ),
      ).toBe(true),
    );
  });
});
