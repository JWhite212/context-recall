import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";

import {
  ApiError,
  createAutomationRule,
  createInsightDefinition,
  describeApiError,
  exportMeeting,
  generatePrepForEvent,
  getCalendarEvents,
  getCalendars,
  getHealth,
  getMeetingTags,
  getStatus,
  getUpcomingPrepList,
  getPrepByEvent,
  getPreparedEventUids,
  setAuthToken,
  triggerCalendarSync,
  setMeetingTags,
} from "../api";

/**
 * Tests for the HTTP client's timeout, abort, and 401-recovery behaviour.
 * Each test installs its own `globalThis.fetch` stub so we can drive the
 * branches deterministically without a real network.
 */

const originalFetch = globalThis.fetch;

beforeEach(() => {
  // Start every test with a known token so the Authorization header is set.
  setAuthToken("initial-token");
  vi.mocked(invoke).mockClear();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("ApiError", () => {
  it("captures status, detail, and retried flag", () => {
    const err = new ApiError(500, "boom", true);
    expect(err).toBeInstanceOf(Error);
    expect(err.status).toBe(500);
    expect(err.detail).toBe("boom");
    expect(err.retried).toBe(true);
    expect(err.message).toBe("API 500: boom");
    expect(err.name).toBe("ApiError");
  });
});

describe("request<T> — happy path", () => {
  it("sets Authorization header from the cached token", async () => {
    const fetchSpy = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({ status: "ok", uptime: 1, version: "x" }),
    );
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    await getHealth();

    const init = fetchSpy.mock.calls[0][1] as RequestInit & {
      headers: Record<string, string>;
    };
    expect(init.headers["Authorization"]).toBe("Bearer initial-token");
    expect(init.signal).toBeInstanceOf(AbortSignal);
  });
});

describe("request<T> — non-401 errors", () => {
  it("throws ApiError with the server-supplied detail", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse({ detail: "nope" }, 500),
    ) as unknown as typeof fetch;

    await expect(getHealth()).rejects.toMatchObject({
      status: 500,
      detail: "nope",
      retried: false,
      name: "ApiError",
    });
  });

  it("falls back to statusText when the body is not JSON", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response("oops", {
          status: 502,
          statusText: "Bad Gateway",
        }),
    ) as unknown as typeof fetch;

    await expect(getHealth()).rejects.toMatchObject({
      status: 502,
      detail: "Bad Gateway",
    });
  });
});

describe("request<T> — 401 retry", () => {
  it("re-reads the token via invoke and retries the request once", async () => {
    vi.mocked(invoke).mockImplementation(async (cmd: string) => {
      if (cmd === "read_auth_token") return "fresh-token";
      return null;
    });

    const fetchSpy = vi.fn(
      async (_url: string, _init?: RequestInit) => new Response(),
    );
    fetchSpy.mockResolvedValueOnce(jsonResponse({ detail: "stale" }, 401));
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ status: "ok", uptime: 1, version: "x" }),
    );
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    const result = await getHealth();

    expect(result).toEqual({ status: "ok", uptime: 1, version: "x" });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(invoke).toHaveBeenCalledWith("read_auth_token");

    // Second call must carry the refreshed token.
    const secondInit = fetchSpy.mock.calls[1][1] as RequestInit & {
      headers: Record<string, string>;
    };
    expect(secondInit.headers["Authorization"]).toBe("Bearer fresh-token");
  });

  it("throws ApiError with retried=true when retry also returns 401", async () => {
    vi.mocked(invoke).mockResolvedValue("another-token" as unknown as never);

    const fetchSpy = vi.fn(async () =>
      jsonResponse({ detail: "still bad" }, 401),
    );
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    await expect(getStatus()).rejects.toMatchObject({
      status: 401,
      detail: "still bad",
      retried: true,
    });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });

  it("retries even when reading the fresh token throws", async () => {
    vi.mocked(invoke).mockImplementation(async () => {
      throw new Error("tauri unavailable");
    });

    const fetchSpy = vi.fn();
    fetchSpy.mockResolvedValueOnce(jsonResponse({ detail: "stale" }, 401));
    fetchSpy.mockResolvedValueOnce(
      jsonResponse({ status: "ok", uptime: 1, version: "x" }),
    );
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    await expect(getHealth()).resolves.toBeDefined();
    expect(fetchSpy).toHaveBeenCalledTimes(2);
  });
});

describe("request<T> — timeout / abort", () => {
  it("propagates an AbortError as ApiError(status=0, timeout message)", async () => {
    globalThis.fetch = vi.fn(async () => {
      const err = new Error("aborted");
      (err as Error & { name: string }).name = "AbortError";
      throw err;
    }) as unknown as typeof fetch;

    await expect(getHealth()).rejects.toMatchObject({
      status: 0,
      retried: false,
    });
    await expect(getHealth()).rejects.toThrow(/timed out/i);
  });

  it("wraps generic network errors as ApiError(status=0)", async () => {
    globalThis.fetch = vi.fn(async () => {
      throw new TypeError("Failed to fetch");
    }) as unknown as typeof fetch;

    await expect(getHealth()).rejects.toMatchObject({
      status: 0,
      detail: "Failed to fetch",
    });
  });
});

describe("requestRaw consumers", () => {
  it("exportMeeting returns body text and flows through the same contract", async () => {
    globalThis.fetch = vi.fn(
      async () =>
        new Response("# transcript", {
          status: 200,
          headers: { "content-type": "text/markdown" },
        }),
    ) as unknown as typeof fetch;

    await expect(exportMeeting("abc")).resolves.toBe("# transcript");
  });

  it("exportMeeting throws ApiError on non-OK status", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response("nope", { status: 500 }),
    ) as unknown as typeof fetch;

    await expect(exportMeeting("abc")).rejects.toBeInstanceOf(ApiError);
  });

  it("getPrepByEvent returns null for a 204 No Content response", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 204 }),
    ) as unknown as typeof fetch;

    await expect(getPrepByEvent("EK1:1000")).resolves.toBeNull();
  });
});

describe("insight definitions", () => {
  it("createInsightDefinition POSTs the body", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(JSON.stringify({ id: "d1" }), {
          status: 201,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;

    await createInsightDefinition({ name: "Risks", prompt: "p" });

    const call = calls.find((c) => c.init?.method === "POST");
    expect(call?.url).toContain("/api/insight-definitions");
    expect(JSON.parse(call?.init?.body as string)).toEqual({
      name: "Risks",
      prompt: "p",
    });
  });

  it("creates a structured insight definition with fields", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(JSON.stringify({ id: "x" }), {
          status: 201,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;

    await createInsightDefinition({
      name: "Client Call",
      prompt: "p",
      enabled: true,
      output_mode: "structured",
      fields: [{ key: "go_live", label: "Go-live", type: "date" }],
    });

    const call = calls.find((c) => c.init?.method === "POST");
    const body = JSON.parse(call?.init?.body as string);
    expect(body.output_mode).toBe("structured");
    expect(body.fields[0].type).toBe("date");
  });
});

describe("automation rules", () => {
  it("createAutomationRule POSTs the body", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(JSON.stringify({ id: "r1" }), {
          status: 201,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;

    await createAutomationRule({
      name: "R",
      match_mode: "all",
      conditions: [{ field: "tag", value: "Type/Discovery" }],
      actions: [{ type: "apply_tag", tags: ["Reviewed"] }],
    });

    const call = calls.find((c) => c.init?.method === "POST");
    expect(call?.url).toContain("/api/automation-rules");
    expect(JSON.parse(call?.init?.body as string).name).toBe("R");
  });
});

describe("calendar import", () => {
  it("getCalendarEvents requests the range", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ events: [], count: 0 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;

    await getCalendarEvents(100, 200);
    expect(calls[0]).toContain("/api/calendar/events?start=100&end=200");
  });

  it("getCalendars requests the calendars list", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(
        JSON.stringify({ calendars: [{ id: "cal1", title: "Work" }] }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    }) as unknown as typeof fetch;

    await getCalendars();
    expect(calls[0]).toContain("/api/calendar/calendars");
  });

  it("triggerCalendarSync POSTs", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(JSON.stringify({ synced: 3 }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    ) as unknown as typeof fetch;

    const res = await triggerCalendarSync();
    expect(res.synced).toBe(3);
    const call = calls.find((c) => c.init?.method === "POST");
    expect(call?.url).toContain("/api/calendar/sync");
  });
});

describe("auto-prep", () => {
  it("getUpcomingPrepList requests the list", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    await getUpcomingPrepList();
    expect(calls[0]).toContain("/api/prep/upcoming-list");
  });

  it("getPreparedEventUids requests prepared-events", async () => {
    const calls: string[] = [];
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      calls.push(input.toString());
      return new Response(JSON.stringify({ event_uids: ["EK1:1000"] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    const res = await getPreparedEventUids();
    expect(res.event_uids).toEqual(["EK1:1000"]);
    expect(calls[0]).toContain("/api/prep/prepared-events");
  });
});

describe("prep by-event", () => {
  it("getPrepByEvent returns null on 204", async () => {
    globalThis.fetch = vi.fn(
      async () => new Response(null, { status: 204 }),
    ) as unknown as typeof fetch;
    const res = await getPrepByEvent("EK1:1000");
    expect(res).toBeNull();
  });

  it("generatePrepForEvent POSTs the event body", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    globalThis.fetch = vi.fn(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        calls.push({ url: input.toString(), init });
        return new Response(
          JSON.stringify({ id: "p1", content_markdown: "x" }),
          {
            status: 201,
            headers: { "content-type": "application/json" },
          },
        );
      },
    ) as unknown as typeof fetch;
    await generatePrepForEvent({
      event_uid: "EK1:1000",
      title: "Sync",
      attendees: [{ name: "A", email: "a@x.com" }],
      attendee_names: ["A"],
      end_ts: 123,
      series_id: null,
    });
    const call = calls.find((c) => c.init?.method === "POST");
    expect(call?.url).toContain("/api/prep/by-event/generate");
    expect(JSON.parse(call?.init?.body as string).event_uid).toBe("EK1:1000");
  });
});

describe("meeting tags", () => {
  it("setMeetingTags PATCHes the tags array", async () => {
    const fetchSpy = vi.fn(async (_url: string, _init?: RequestInit) =>
      jsonResponse({}),
    );
    globalThis.fetch = fetchSpy as unknown as typeof fetch;

    await setMeetingTags("m1", ["a", "b"]);

    const [url, init] = fetchSpy.mock.calls[0] as [string, RequestInit];
    expect(url).toContain("/api/meetings/m1/tags");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ tags: ["a", "b"] });
  });

  it("getMeetingTags returns the tags array", async () => {
    globalThis.fetch = vi.fn(async () =>
      jsonResponse({ tags: ["x", "y"] }),
    ) as unknown as typeof fetch;

    expect(await getMeetingTags()).toEqual(["x", "y"]);
  });
});

describe("describeApiError", () => {
  it("appends the backend detail for an ApiError", () => {
    expect(
      describeApiError(
        new ApiError(422, "Invalid speaker_id format"),
        "Failed to assign person",
      ),
    ).toBe("Failed to assign person: Invalid speaker_id format");
  });

  it("falls back to the message for a non-ApiError", () => {
    expect(describeApiError(new Error("boom"), "Failed to assign person")).toBe(
      "Failed to assign person",
    );
  });
});
