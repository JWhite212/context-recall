import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { CalendarLinkCard } from "../CalendarLinkCard";
import type { Meeting } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";

const base: Meeting = {
  id: "m1",
  title: "Amelia Monthly Check-In",
  started_at: 1_700_000_000,
  ended_at: 1_700_001_000,
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

describe("CalendarLinkCard", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 204 }),
    ) as unknown as typeof fetch;
  });

  it("shows Unlink when linked", () => {
    render(
      <CalendarLinkCard
        meeting={{
          ...base,
          calendar_event_uid: "EK1:1000",
          calendar_event_title: "Jamie - Quick Catch-Up",
        }}
      />,
      { wrapper: makeWrapper() },
    );
    expect(screen.getByText(/Jamie - Quick Catch-Up/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /unlink/i })).toBeInTheDocument();
  });

  it("shows a Link button when unlinked", () => {
    render(<CalendarLinkCard meeting={base} />, { wrapper: makeWrapper() });
    expect(
      screen.getByRole("button", { name: /link to calendar event/i }),
    ).toBeInTheDocument();
  });
});
