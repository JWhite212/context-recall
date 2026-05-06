import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useWebSocket } from "../useWebSocket";

describe("useWebSocket", () => {
  let WebSocketSpy: ReturnType<typeof vi.fn>;
  let originalWebSocket: typeof WebSocket;

  beforeEach(() => {
    originalWebSocket = globalThis.WebSocket;
    WebSocketSpy = vi.fn(() => ({
      readyState: 0,
      send: vi.fn(),
      close: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
    }));
    // The hook references WebSocket.OPEN, so the spy needs the readyState
    // constants on its constructor.
    Object.assign(WebSocketSpy, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
    globalThis.WebSocket = WebSocketSpy as unknown as typeof WebSocket;
  });

  afterEach(() => {
    globalThis.WebSocket = originalWebSocket;
  });

  it("opens a single WebSocket on mount", () => {
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));

    expect(WebSocketSpy).toHaveBeenCalledTimes(1);
  });
});
