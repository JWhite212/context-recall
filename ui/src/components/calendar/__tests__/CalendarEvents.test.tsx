import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgendaList } from "../AgendaList";
import type { CalendarEvent } from "../../../lib/types";

function makeWrapper() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

const EVENT: CalendarEvent = {
  event_uid: "EK1:1700000000",
  title: "Upcoming standup",
  start_ts: 1_700_000_000,
  end_ts: 1_700_003_600,
  attendees: [
    { name: "Alice", email: "a@x.com" },
    { name: "Bob", email: "b@x.com" },
  ],
  organizer: null,
  join_url: "",
  meeting_id: "",
  calendar_name: "Work",
};

describe("AgendaList with events", () => {
  it("renders upcoming events alongside meetings", () => {
    render(
      <QueryClientProvider client={makeWrapper()}>
        <MemoryRouter>
          <AgendaList meetings={[]} events={[EVENT]} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByText("Upcoming standup")).toBeInTheDocument();
  });
});
