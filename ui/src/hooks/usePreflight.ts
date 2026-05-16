import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../lib/constants";
import { getAuthToken } from "../lib/api";

/**
 * Shape of the /api/preflight response. The endpoint is owned by Unit 4 and
 * may not exist yet — the hook degrades gracefully on 404. All fields are
 * optional so we ignore anything we don't recognise.
 */
export interface PreflightCheck {
  name: string;
  status: "pass" | "warn" | "fail";
  message?: string;
}

export interface PreflightData {
  checks?: PreflightCheck[];
  ok?: boolean;
  [key: string]: unknown;
}

interface UsePreflight {
  data: PreflightData | null;
  isLoading: boolean;
  /** True when the daemon returned 404 — Unit 4 hasn't merged yet. */
  notImplemented: boolean;
  error: string | null;
}

/**
 * Fetches /api/preflight. Returns `notImplemented` when the endpoint is
 * missing (HTTP 404) so the diagnostics panel can hide its section without
 * showing an error.
 */
export function usePreflight(): UsePreflight {
  const [data, setData] = useState<PreflightData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notImplemented, setNotImplemented] = useState(false);
  const cancelledRef = useRef(false);

  const fetchPreflight = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    setNotImplemented(false);
    const headers: Record<string, string> = {};
    const token = getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      const res = await fetch(`${API_BASE}/api/preflight`, {
        headers,
        cache: "no-store",
      });
      if (res.status === 404) {
        if (!cancelledRef.current) setNotImplemented(true);
      } else if (!res.ok) {
        if (!cancelledRef.current) setError(`HTTP ${res.status}`);
      } else {
        const body = (await res.json()) as PreflightData;
        if (!cancelledRef.current) setData(body);
      }
    } catch (e) {
      if (!cancelledRef.current) setError(String(e));
    } finally {
      if (!cancelledRef.current) setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    fetchPreflight();
    return () => {
      cancelledRef.current = true;
    };
  }, [fetchPreflight]);

  return { data, isLoading, notImplemented, error };
}
