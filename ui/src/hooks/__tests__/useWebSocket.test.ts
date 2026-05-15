import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useWebSocket } from "../useWebSocket";
import { setAuthToken } from "../../lib/api";

interface FakeSocket {
  readyState: number;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  onopen: ((e?: unknown) => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e?: unknown) => void) | null;
  onerror: ((e?: unknown) => void) | null;
}

describe("useWebSocket", () => {
  let WebSocketCtor: ReturnType<typeof vi.fn>;
  let originalWebSocket: typeof WebSocket;
  let sockets: FakeSocket[];

  beforeEach(() => {
    vi.useFakeTimers();
    sockets = [];
    originalWebSocket = globalThis.WebSocket;
    WebSocketCtor = vi.fn(function (this: FakeSocket) {
      this.readyState = 0;
      this.send = vi.fn();
      this.close = vi.fn(() => {
        this.readyState = 3;
        this.onclose?.();
      });
      this.onopen = null;
      this.onmessage = null;
      this.onclose = null;
      this.onerror = null;
      sockets.push(this);
    });
    Object.assign(WebSocketCtor, {
      CONNECTING: 0,
      OPEN: 1,
      CLOSING: 2,
      CLOSED: 3,
    });
    globalThis.WebSocket = WebSocketCtor as unknown as typeof WebSocket;
    setAuthToken(null);
  });

  afterEach(() => {
    vi.useRealTimers();
    globalThis.WebSocket = originalWebSocket;
    setAuthToken(null);
  });

  it("does NOT open a WebSocket until a token is available", () => {
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));

    expect(WebSocketCtor).not.toHaveBeenCalled();
  });

  it("opens a single WebSocket once a token is set", () => {
    setAuthToken("token-a");
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));

    expect(WebSocketCtor).toHaveBeenCalledTimes(1);
  });

  it("polls for the token and connects after it appears post-mount", () => {
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));

    expect(WebSocketCtor).not.toHaveBeenCalled();

    act(() => {
      setAuthToken("token-late");
    });
    act(() => {
      vi.advanceTimersByTime(500);
    });

    // Token may arrive via the subscriber path before the poll runs, but
    // either way only ONE socket should be opened.
    expect(WebSocketCtor).toHaveBeenCalledTimes(1);
  });

  it("resets the backoff counter only on the first inbound message, not on open", () => {
    setAuthToken("token-a");
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));

    // Server accepts the socket but immediately closes after onopen (no
    // inbound traffic). The hook must NOT reset the backoff on open alone.
    const s1 = sockets[0];
    act(() => {
      s1.onopen?.();
    });
    // Don't double-trigger onclose via close(); simulate server-side close.
    act(() => {
      s1.onclose?.();
    });

    // First reconnect runs after BASE_DELAY * 2^0 + jitter (3000-4000ms).
    act(() => {
      vi.advanceTimersByTime(4100);
    });
    expect(WebSocketCtor).toHaveBeenCalledTimes(2);

    // Second socket also opens-then-closes without messages: backoff must
    // grow rather than reset, so the next attempt requires 6000+ms.
    const s2 = sockets[1];
    act(() => {
      s2.onopen?.();
      s2.onclose?.();
    });

    act(() => {
      vi.advanceTimersByTime(4100);
    });
    expect(WebSocketCtor).toHaveBeenCalledTimes(2);
    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(WebSocketCtor).toHaveBeenCalledTimes(3);

    // Now the third socket receives an actual message — backoff resets.
    const s3 = sockets[2];
    act(() => {
      s3.onopen?.();
      s3.onmessage?.({ data: JSON.stringify({ type: "pong" }) });
      s3.onclose?.();
    });

    act(() => {
      vi.advanceTimersByTime(4100);
    });
    expect(WebSocketCtor).toHaveBeenCalledTimes(4);
  });

  it("closes the existing socket and reopens when the token rotates", () => {
    setAuthToken("token-a");
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));
    expect(WebSocketCtor).toHaveBeenCalledTimes(1);

    const first = sockets[0];
    const closeSpy = first.close;

    act(() => {
      setAuthToken("token-b");
    });

    expect(closeSpy).toHaveBeenCalled();
    expect(WebSocketCtor).toHaveBeenCalledTimes(2);
  });

  it("force-closes the socket on error so onclose schedules the reconnect", () => {
    setAuthToken("token-a");
    const onEvent = vi.fn();
    renderHook(() => useWebSocket(onEvent));

    const s = sockets[0];
    act(() => {
      s.onerror?.();
    });

    expect(s.close).toHaveBeenCalled();
    // close() triggers onclose which schedules a reconnect.
    act(() => {
      vi.advanceTimersByTime(4100);
    });
    expect(WebSocketCtor).toHaveBeenCalledTimes(2);
  });
});
