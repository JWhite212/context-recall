import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { PrepModal } from "../PrepModal";
import { makeWrapper } from "../../../test/queryWrapper";


describe("PrepModal", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            id: "p1",
            content_markdown: "## Prep\nAlice notes",
            expires_at: 9e9,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
    ) as unknown as typeof fetch;
  });

  it("renders the briefing markdown", async () => {
    render(<PrepModal eventUid="EK1:1000" title="Sync" onClose={() => {}} />, {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(screen.getByText("Prep")).toBeInTheDocument());
  });
});
