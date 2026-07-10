import { describe, it, expect } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { WeekTimeline } from "../WeekTimeline";
import type { CalendarEvent } from "../../../lib/types";

// Wednesday, 15:00 UTC — well inside the visible 07:00-22:00 window.
const EVENT: CalendarEvent = {
  event_uid: "EK1:1700060400",
  title: "Design sync",
  start_ts: 1_700_060_400,
  end_ts: 1_700_064_000,
  attendees: [{ name: "Alice", email: "a@x.com" }],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "Work",
};

describe("WeekTimeline with events", () => {
  it("renders upcoming events through the clickable UpcomingEventCard", () => {
    render(
      <MemoryRouter>
        <WeekTimeline
          currentDate={new Date(EVENT.start_ts * 1000)}
          meetings={[]}
          events={[EVENT]}
        />
      </MemoryRouter>,
    );
    const button = screen.getByRole("button", { name: /Design sync/i });
    expect(button).toBeInTheDocument();

    // UpcomingEventCard's detail popover opens on click.
    fireEvent.click(button);
    expect(screen.getByText("Alice")).toBeInTheDocument();
  });
});
