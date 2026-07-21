import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { EventCard } from "../EventCard";
import type { Meeting } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";

const base: Meeting = {
  id: "m1",
  title: "Amelia Monthly Check-In",
  started_at: 1000,
  ended_at: 2000,
  duration_seconds: 1380,
  status: "complete",
  audio_path: null,
  transcript_json: null,
  summary_markdown: null,
  tags: [],
  language: null,
  word_count: null,
  label: "",
  created_at: 0,
  updated_at: 0,
  calendar_event_title: "",
  attendees_json: "[]",
  calendar_confidence: 0,
  teams_join_url: "",
  teams_meeting_id: "",
};

function renderCard(m: Meeting) {
  const Wrapper = makeWrapper();
  return render(
    <Wrapper>
      <EventCard meeting={m} />
    </Wrapper>,
  );
}

describe("EventCard link affordances", () => {
  it("shows the linked-entry annotation when linked", () => {
    renderCard({
      ...base,
      calendar_event_uid: "EK1:1000",
      calendar_event_title: "Jamie - Quick Catch-Up",
    });
    expect(screen.getByText(/Jamie - Quick Catch-Up/)).toBeInTheDocument();
  });

  it("exposes a link menu when not linked", () => {
    renderCard(base);
    fireEvent.click(screen.getByRole("button", { name: /link options/i }));
    expect(screen.getByText(/Link to calendar event/i)).toBeInTheDocument();
  });

  it("excludes calendar entries already linked to another recording", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/calendar/events")) {
        return new Response(
          JSON.stringify({
            events: [
              {
                event_uid: "EK-claimed",
                title: "Already Claimed Sync",
                start_ts: 1000,
                end_ts: 1600,
                attendees: [],
                organizer: null,
                join_url: "",
                meeting_id: "",
                calendar_name: "Work",
              },
              {
                event_uid: "EK-free",
                title: "Unclaimed Sync",
                start_ts: 1000,
                end_ts: 1600,
                attendees: [],
                organizer: null,
                join_url: "",
                meeting_id: "",
                calendar_name: "Work",
              },
            ],
            count: 2,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.includes("/api/calendar/meetings")) {
        return new Response(
          JSON.stringify({
            meetings: [{ ...base, id: "m2", calendar_event_uid: "EK-claimed" }],
            count: 1,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response(null, { status: 204 });
    }) as unknown as typeof fetch;

    renderCard(base);
    fireEvent.click(screen.getByRole("button", { name: /link options/i }));
    fireEvent.click(screen.getByText(/Link to calendar event/i));

    await waitFor(() =>
      expect(screen.getByText(/Unclaimed Sync/)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/Already Claimed Sync/)).not.toBeInTheDocument();
  });
});
