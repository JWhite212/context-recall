import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { InsightsSection } from "../InsightsSection";
import { makeWrapper } from "../../../test/queryWrapper";


describe("InsightsSection", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/insight-definitions")) {
        return new Response(
          JSON.stringify([
            {
              id: "d1",
              name: "Risks",
              prompt: "List risks",
              enabled: true,
              created_at: 1,
              updated_at: 1,
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

  it("lists existing insight definitions", async () => {
    render(<InsightsSection id="insights" />, { wrapper: makeWrapper() });
    await waitFor(() => expect(screen.getByText("Risks")).toBeInTheDocument());
  });
});
