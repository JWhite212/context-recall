import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../lib/constants";
import { getAuthToken } from "../lib/api";

/**
 * Shape of the /api/diagnostics response. All fields are optional so the
 * UI gracefully ignores fields the daemon does not yet emit.
 */
export interface DiagnosticsData {
  platform?: string;
  apple_silicon?: boolean;
  blackhole_found?: boolean;
  microphone_available?: boolean;
  audio_output_devices?: string[];
  ollama_reachable?: boolean;
  selected_ollama_model_available?: boolean;
  mlx_available?: boolean;
  whisper_model_cached?: boolean;
  database_accessible?: boolean;
  logs_dir_writable?: boolean;
  app_support_dir_writable?: boolean;
  ffmpeg_available?: boolean;
  active_profile?: string;
  [key: string]: unknown;
}

interface UseDiagnostics {
  data: DiagnosticsData | null;
  isLoading: boolean;
  error: string | null;
  refetch: () => void;
}

/**
 * Fetches /api/diagnostics with bearer auth. Uses plain fetch instead of
 * React Query so the hook can run on the pre-connection screen, which
 * renders outside the QueryClientProvider tree.
 */
export function useDiagnostics(): UseDiagnostics {
  const [data, setData] = useState<DiagnosticsData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const cancelledRef = useRef(false);

  const fetchDiagnostics = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    const headers: Record<string, string> = {};
    const token = getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;

    try {
      const res = await fetch(`${API_BASE}/api/diagnostics`, {
        headers,
        cache: "no-store",
      });
      if (!res.ok) {
        if (!cancelledRef.current) setError(`HTTP ${res.status}`);
      } else {
        const body = (await res.json()) as DiagnosticsData;
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
    fetchDiagnostics();
    return () => {
      cancelledRef.current = true;
    };
  }, [fetchDiagnostics]);

  return { data, isLoading, error, refetch: fetchDiagnostics };
}
