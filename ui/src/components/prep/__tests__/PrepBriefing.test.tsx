import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { PrepBriefing } from "../PrepBriefing";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

describe("PrepBriefing upcoming list", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/prep/upcoming-list")) {
        return new Response(
          JSON.stringify([
            {
              id: "p1",
              meeting_id: null,
              series_id: null,
              content_markdown: "## Standup prep\nAlice notes",
              attendees_json: "[]",
              related_meeting_ids_json: "[]",
              open_action_items_json: "[]",
              generated_at: 1,
              expires_at: 9999999999,
            },
          ]),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("[]", {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
  });

  it("renders the list of upcoming briefings", async () => {
    render(<PrepBriefing />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(screen.getByText("Standup prep")).toBeInTheDocument(),
    );
  });
});
