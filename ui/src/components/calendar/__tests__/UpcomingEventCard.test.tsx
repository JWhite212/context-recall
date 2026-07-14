import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { UpcomingEventCard } from "../UpcomingEventCard";
import type { CalendarEvent } from "../../../lib/types";
import { makeWrapper } from "../../../test/queryWrapper";


const EVENT: CalendarEvent = {
  event_uid: "EK1:1000",
  title: "Design sync",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [
    { name: "Alice", email: "a@x.com" },
    { name: "Bob", email: "b@x.com" },
  ],
  organizer: null,
  join_url: "https://teams.microsoft.com/l/meetup-join/x",
  meeting_id: "19:abc",
  calendar_name: "Work",
};

describe("UpcomingEventCard", () => {
  it("renders the event title", () => {
    render(<UpcomingEventCard event={EVENT} />, { wrapper: makeWrapper() });
    expect(screen.getByText("Design sync")).toBeInTheDocument();
  });

  it("reveals attendees in a popover on click", () => {
    render(<UpcomingEventCard event={EVENT} />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByRole("button", { name: /Design sync/i }));
    expect(screen.getByText("Alice")).toBeInTheDocument();
    expect(screen.getByText(/Join/i)).toBeInTheDocument();
  });
});
