import { describe, it, expect } from "vitest";
import { collapseLinkedEvents } from "../CalendarView";
import type { Meeting, CalendarEvent } from "../../../lib/types";

const meeting = (over: Partial<Meeting>): Meeting =>
  ({
    id: "m1",
    title: "T",
    started_at: 1000,
    ended_at: 2000,
    duration_seconds: 1000,
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
    ...over,
  }) as Meeting;

const ev = (uid: string): CalendarEvent => ({
  event_uid: uid,
  title: "E",
  start_ts: 1000,
  end_ts: 2000,
  attendees: [],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "",
});

describe("collapseLinkedEvents", () => {
  it("drops events already linked to a recording", () => {
    const out = collapseLinkedEvents(
      [meeting({ calendar_event_uid: "EK1:1000" })],
      [ev("EK1:1000"), ev("EK2:2000")],
    );
    expect(out.map((e) => e.event_uid)).toEqual(["EK2:2000"]);
  });

  it("keeps all events when nothing is linked", () => {
    const out = collapseLinkedEvents([meeting({})], [ev("EK1:1000")]);
    expect(out).toHaveLength(1);
  });
});
