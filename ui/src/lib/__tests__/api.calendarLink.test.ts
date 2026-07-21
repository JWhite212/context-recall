import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  linkMeetingToCalendarEvent,
  unlinkMeetingFromCalendarEvent,
} from "../api";
import type { CalendarEvent } from "../types";

const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Quick Catch-Up",
  start_ts: 1000,
  end_ts: 2800,
  attendees: [{ name: "Jamie", email: "j@x.com" }],
  organizer: null,
  join_url: "https://teams/x",
  meeting_id: "19:mtg",
  calendar_name: "Work",
};

describe("calendar-link api", () => {
  let calls: { url: string; method?: string; body?: string }[];
  beforeEach(() => {
    calls = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({
          url: input.toString(),
          method: init?.method,
          body: init?.body as string,
        });
        return new Response(
          JSON.stringify({ id: "m1", calendar_event_uid: "EK1:1000" }),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        );
      },
    ) as unknown as typeof fetch;
  });

  it("PUTs the event payload to the link endpoint", async () => {
    await linkMeetingToCalendarEvent("m1", EVENT);
    expect(calls[0].url).toContain("/api/meetings/m1/calendar-link");
    expect(calls[0].method).toBe("PUT");
    expect(JSON.parse(calls[0].body!).event_uid).toBe("EK1:1000");
  });

  it("DELETEs to unlink", async () => {
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), method: init?.method });
        return new Response(null, { status: 204 });
      },
    ) as unknown as typeof fetch;
    await unlinkMeetingFromCalendarEvent("m1");
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toContain("/api/meetings/m1/calendar-link");
  });
});
