import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";


const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Standup",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "Work",
};

describe("UpcomingEventCard prep badge", () => {
  it("shows a Prep ready badge when the uid is prepared", () => {
    render(
      <UpcomingEventCard event={EVENT} preparedUids={new Set(["EK1:1000"])} />,
      { wrapper: makeWrapper() },
    );
    expect(screen.getByText(/Prep ready/i)).toBeInTheDocument();
  });

  it("hides the badge when not prepared", () => {
    render(<UpcomingEventCard event={EVENT} preparedUids={new Set()} />, {
      wrapper: makeWrapper(),
    });
    expect(screen.queryByText(/Prep ready/i)).not.toBeInTheDocument();
  });
});
