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


function liveEvent(): CalendarEvent {
  const now = Math.floor(Date.now() / 1000);
  return {
    event_uid: "EK1:1000",
    title: "Standup",
    start_ts: now - 60,
    end_ts: now + 600,
    attendees: [{ name: "Alice", email: "a@x.com" }],
    organizer: null,
    join_url: "",
    meeting_id: "",
    calendar_name: "Work",
  };
}

describe("UpcomingEventCard actions", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/prep/by-event/generate")) {
        return new Response(
          JSON.stringify({ id: "p1", content_markdown: "x" }),
          {
            status: 201,
            headers: { "content-type": "application/json" },
          },
        );
      }
      return new Response(null, { status: 204 });
    }) as unknown as typeof fetch;
  });

  it("shows Generate when not prepared and posts on click", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ id: "p1", content_markdown: "x" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    render(<UpcomingEventCard event={liveEvent()} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByRole("button", { name: /Standup/i }));
    fireEvent.click(screen.getByText(/Generate prep/i));
    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/prep/by-event/generate"))).toBe(
        true,
      ),
    );
  });

  it("Record button confirms then starts recording when live", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(
        JSON.stringify({ status: "recording", started_at: 1 }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    }) as unknown as typeof fetch;
    render(<UpcomingEventCard event={liveEvent()} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByRole("button", { name: /Standup/i }));
    fireEvent.click(screen.getByText(/Record this meeting/i));
    fireEvent.click(screen.getByText(/Start recording\?/i)); // two-step confirm
    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/record/start"))).toBe(true),
    );
  });
});
