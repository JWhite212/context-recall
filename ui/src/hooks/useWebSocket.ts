import { useEffect, useRef, useCallback, useState } from "react";
import { WS_URL } from "../lib/constants";
import { getAuthToken, subscribeAuthToken } from "../lib/api";
import type { WSEvent } from "../lib/types";

const BASE_DELAY = 3000;
const MAX_DELAY = 30000;

export function useWebSocket(onEvent: (event: WSEvent) => void) {
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const attemptRef = useRef(0);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  const scheduleReconnect = useCallback((reconnect: () => void) => {
    const delay = Math.min(
      BASE_DELAY * Math.pow(2, attemptRef.current),
      MAX_DELAY,
    );
    const jitter = Math.random() * 1000;
    attemptRef.current++;
    clearTimeout(reconnectTimer.current);
    reconnectTimer.current = setTimeout(reconnect, delay + jitter);
  }, []);

  const connect = useCallback(() => {
    // Bail if a socket is already in-flight (CONNECTING or OPEN). The poll
    // loop and the token-change subscriber can both call connect() — only
    // one needs to succeed.
    const existing = wsRef.current;
    if (
      existing &&
      (existing.readyState === WebSocket.CONNECTING ||
        existing.readyState === WebSocket.OPEN)
    ) {
      return;
    }

    // Do not open a socket until a token is available — the daemon will close
    // an unauthenticated connection with 4001 and trigger a retry storm.
    const token = getAuthToken();
    if (!token) return;

    try {
      const ws = new WebSocket(WS_URL);

      ws.onopen = () => {
        // Send auth token as first message instead of in URL.
        ws.send(JSON.stringify({ type: "auth", token }));
        setConnected(true);
      };

      ws.onmessage = (e) => {
        // First inbound message proves the server accepted our auth; only
        // then do we treat the connection as healthy and clear the backoff.
        attemptRef.current = 0;
        try {
          const event = JSON.parse(e.data) as WSEvent;
          onEventRef.current(event);
        } catch {
          // Ignore malformed messages.
        }
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;
        scheduleReconnect(connect);
      };

      ws.onerror = () => {
        // Force close so `onclose` always runs and schedules reconnect.
        ws.close();
      };

      wsRef.current = ws;
    } catch {
      scheduleReconnect(connect);
    }
  }, [scheduleReconnect]);

  // Drive the initial connect: poll for a token if one isn't set yet,
  // then connect. The token-change subscriber below also drives the first
  // connect when the token arrives async — whichever wins, `connect()` is
  // idempotent.
  useEffect(() => {
    let cancelled = false;
    let pollTimer: ReturnType<typeof setTimeout> | undefined;

    const tryConnect = () => {
      if (cancelled) return;
      if (getAuthToken()) {
        connect();
        return;
      }
      pollTimer = setTimeout(tryConnect, 200);
    };

    tryConnect();

    return () => {
      cancelled = true;
      clearTimeout(pollTimer);
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  // Reconnect when the token changes (e.g. rotation). Close the existing
  // socket so the daemon stops accepting traffic on the old token, and
  // reset the backoff so the rotation reconnect happens immediately.
  useEffect(() => {
    return subscribeAuthToken((next) => {
      clearTimeout(reconnectTimer.current);
      attemptRef.current = 0;
      const existing = wsRef.current;
      wsRef.current = null;
      existing?.close();
      if (next) connect();
    });
  }, [connect]);

  return { connected };
}
