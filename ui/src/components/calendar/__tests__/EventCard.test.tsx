import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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
});
