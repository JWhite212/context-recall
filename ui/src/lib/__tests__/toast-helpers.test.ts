import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api";
import { formatApiError, toastApiError } from "../toast-helpers";

describe("formatApiError", () => {
  it("formats a generic ApiError as `detail (status N)`", () => {
    expect(formatApiError(new ApiError(500, "boom"))).toBe("boom (status 500)");
  });

  it("uses a friendlier message for unauthenticated requests", () => {
    expect(formatApiError(new ApiError(401, "x"))).toBe(
      "Authentication required.",
    );
  });

  it("surfaces the retried-401 case distinctly", () => {
    const msg = formatApiError(new ApiError(401, "x", true));
    expect(msg).toMatch(/Authentication failed/i);
  });

  it("uses detail directly for status=0 (network/timeout) failures", () => {
    expect(
      formatApiError(new ApiError(0, "Request timed out after 30000ms")),
    ).toBe("Request timed out after 30000ms");
  });

  it("falls back to err.message for plain Error values", () => {
    expect(formatApiError(new Error("plain"))).toBe("plain");
  });

  it("stringifies anything else", () => {
    expect(formatApiError("oops")).toBe("oops");
    expect(formatApiError(42)).toBe("42");
  });
});

describe("toastApiError", () => {
  it("calls toast.error with the formatted message", () => {
    const sink = { error: vi.fn() };
    toastApiError(sink, new ApiError(404, "missing"));
    expect(sink.error).toHaveBeenCalledWith("missing (status 404)");
  });
});
