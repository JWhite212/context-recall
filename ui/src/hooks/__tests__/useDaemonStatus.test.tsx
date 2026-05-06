import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useDaemonStatus } from "../useDaemonStatus";

function jsonResponse(status: number, body: unknown = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

describe("useDaemonStatus", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("reports daemonRunning when health and status are both ok", async () => {
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = typeof input === "string" ? input : input.toString();
      if (url.includes("/api/health")) return jsonResponse(200, { ok: true });
      if (url.includes("/api/status")) {
        return jsonResponse(200, { state: "idle", active_meeting: null });
      }
      return jsonResponse(404);
    }) as unknown as typeof fetch;

    const { result } = renderHook(() => useDaemonStatus(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => {
      expect(result.current.daemonRunning).toBe(true);
    });
    expect(result.current.state).toBe("idle");
  });
});
