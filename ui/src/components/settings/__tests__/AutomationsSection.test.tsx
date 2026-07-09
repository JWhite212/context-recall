import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { AutomationsSection } from "../AutomationsSection";
import { ToastProvider } from "../../common/Toast";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ToastProvider>{children}</ToastProvider>
    </QueryClientProvider>
  );
}

describe("AutomationsSection", () => {
  beforeEach(() => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = input.toString();
      if (url.includes("/api/automation-rules")) {
        return new Response(
          JSON.stringify([
            {
              id: "r1",
              name: "Tag discovery",
              enabled: true,
              match_mode: "all",
              conditions: [{ field: "tag", value: "Type/Discovery" }],
              actions: [{ type: "apply_tag", tags: ["Reviewed"] }],
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

  it("lists existing automation rules", async () => {
    render(<AutomationsSection id="automations" />, { wrapper: makeWrapper() });
    await waitFor(() =>
      expect(screen.getByText("Tag discovery")).toBeInTheDocument(),
    );
  });
});
