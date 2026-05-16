import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  useDiagnostics,
  type DiagnosticsData,
} from "../../hooks/useDiagnostics";
import { usePreflight, type PreflightData } from "../../hooks/usePreflight";
import { useAppStore } from "../../stores/appStore";

/**
 * Recovery hints keyed by substring match against the latest pipeline.error
 * message. The first matching keyword wins; matches are case-insensitive.
 */
const ERROR_HINTS: { keyword: string; hint: string }[] = [
  {
    keyword: "microphone",
    hint: "Open System Settings → Privacy & Security → Microphone and ensure Context Recall is enabled.",
  },
  {
    keyword: "blackhole",
    hint: "Open Audio MIDI Setup and confirm the BlackHole 2ch device exists and is part of your Multi-Output Device.",
  },
  {
    keyword: "daemon",
    hint: "The recording daemon is unresponsive. Try restarting via the tray menu, then re-run the pipeline.",
  },
];

function recoveryHintFor(error: string | null): string | null {
  if (!error) return null;
  const lower = error.toLowerCase();
  for (const { keyword, hint } of ERROR_HINTS) {
    if (lower.includes(keyword)) return hint;
  }
  return null;
}

/**
 * Each known diagnostics field has a friendly label and a copy used for the
 * "off" state when the value is `false`. Strings without an entry here are
 * rendered with a default label derived from the key.
 */
const FIELD_LABELS: Record<string, { label: string; offCopy?: string }> = {
  platform: { label: "Platform" },
  apple_silicon: { label: "Apple Silicon", offCopy: "Not detected" },
  blackhole_found: { label: "BlackHole audio driver", offCopy: "Not detected" },
  blackhole_candidates: { label: "BlackHole input candidates" },
  configured_blackhole_device: { label: "Configured BlackHole device" },
  configured_blackhole_available: {
    label: "Configured device installed",
    offCopy: "Configured device not installed — see candidates",
  },
  microphone_available: { label: "Microphone", offCopy: "Unavailable" },
  audio_output_devices: { label: "Audio output devices" },
  ollama_reachable: { label: "Ollama reachable", offCopy: "Unavailable" },
  selected_ollama_model_available: {
    label: "Selected Ollama model",
    offCopy: "Not pulled",
  },
  mlx_available: { label: "MLX", offCopy: "Unavailable" },
  whisper_model_cached: {
    label: "Whisper model cached",
    offCopy: "Not cached",
  },
  database_accessible: { label: "Database accessible", offCopy: "Unavailable" },
  logs_dir_writable: {
    label: "Logs directory writable",
    offCopy: "Not writable",
  },
  app_support_dir_writable: {
    label: "App support directory writable",
    offCopy: "Not writable",
  },
  ffmpeg_available: { label: "ffmpeg", offCopy: "Not on PATH" },
  active_profile: { label: "Active profile" },
};

/** Stable order so the panel renders predictably. */
const FIELD_ORDER: string[] = [
  "platform",
  "active_profile",
  "apple_silicon",
  "blackhole_found",
  "configured_blackhole_device",
  "configured_blackhole_available",
  "blackhole_candidates",
  "microphone_available",
  "audio_output_devices",
  "ollama_reachable",
  "selected_ollama_model_available",
  "mlx_available",
  "whisper_model_cached",
  "ffmpeg_available",
  "database_accessible",
  "logs_dir_writable",
  "app_support_dir_writable",
];

function humanise(key: string): string {
  return key
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function buildSummary(data: DiagnosticsData): string {
  const lines: string[] = ["Context Recall diagnostics"];
  for (const key of FIELD_ORDER) {
    if (!(key in data)) continue;
    const label = FIELD_LABELS[key]?.label ?? humanise(key);
    const value = data[key];
    if (Array.isArray(value)) {
      lines.push(
        `- ${label}: ${value.length === 0 ? "(none)" : value.join(", ")}`,
      );
    } else if (typeof value === "boolean") {
      lines.push(`- ${label}: ${value ? "OK" : "FAIL"}`);
    } else if (value === null || value === undefined) {
      lines.push(`- ${label}: (unknown)`);
    } else {
      lines.push(`- ${label}: ${String(value)}`);
    }
  }
  // Include any extra fields the daemon emitted that we don't know about,
  // so support reports don't silently drop new diagnostics.
  for (const [key, value] of Object.entries(data)) {
    if (FIELD_ORDER.includes(key)) continue;
    if (key in FIELD_LABELS) continue;
    lines.push(`- ${humanise(key)}: ${JSON.stringify(value)}`);
  }
  return lines.join("\n");
}

function StatusPill({
  tone,
  children,
}: {
  tone: "ok" | "fail" | "info";
  children: ReactNode;
}) {
  const classes =
    tone === "ok"
      ? "bg-status-idle/20 text-status-idle"
      : tone === "fail"
        ? "bg-status-error/20 text-status-error"
        : "bg-border/40 text-text-secondary";
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${classes}`}
    >
      {children}
    </span>
  );
}

function DeviceList({ devices }: { devices: string[] }) {
  const [expanded, setExpanded] = useState(false);
  if (devices.length === 0) {
    return <StatusPill tone="fail">None</StatusPill>;
  }
  return (
    <div className="flex flex-col items-end gap-1">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="rounded-full bg-border/40 px-2 py-0.5 text-xs font-medium text-text-secondary hover:bg-border/60"
        aria-expanded={expanded}
      >
        {devices.length} device{devices.length === 1 ? "" : "s"}
        <span className="ml-1 opacity-70">{expanded ? "Hide" : "Show"}</span>
      </button>
      {expanded && (
        <ul className="mt-1 max-h-32 w-full overflow-auto rounded-lg bg-background p-2 text-xs text-text-secondary">
          {devices.map((d, i) => (
            <li key={`${d}-${i}`} className="truncate">
              {d}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function FieldValue({ name, value }: { name: string; value: unknown }) {
  if (Array.isArray(value)) {
    return <DeviceList devices={value as string[]} />;
  }
  if (typeof value === "boolean") {
    if (value) return <StatusPill tone="ok">OK</StatusPill>;
    const offCopy = FIELD_LABELS[name]?.offCopy ?? "Unavailable";
    return <StatusPill tone="fail">{offCopy}</StatusPill>;
  }
  if (value === null || value === undefined) {
    return <StatusPill tone="info">Unknown</StatusPill>;
  }
  return <StatusPill tone="info">{String(value)}</StatusPill>;
}

export function DiagnosticsPanel() {
  const { data, isLoading, error, refetch } = useDiagnostics();
  const {
    data: preflight,
    isLoading: preflightLoading,
    notImplemented: preflightNotImplemented,
  } = usePreflight();
  const lastPipelineError = useAppStore((s) => s.lastPipelineError);
  const recoveryHint = recoveryHintFor(lastPipelineError);
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">(
    "idle",
  );
  const copyTimerRef = useRef<number | null>(null);

  const summary = useMemo(() => (data ? buildSummary(data) : ""), [data]);

  useEffect(() => {
    return () => {
      if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    };
  }, []);

  const handleCopy = async () => {
    if (!data) return;
    let nextState: "copied" | "error" = "copied";
    try {
      await navigator.clipboard.writeText(summary);
    } catch {
      nextState = "error";
    }
    setCopyState(nextState);
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    copyTimerRef.current = window.setTimeout(() => setCopyState("idle"), 2000);
  };

  // Render rows in declared order, then any unknown extras the daemon sent.
  const rows = useMemo(() => {
    if (!data) return [] as { key: string; label: string; value: unknown }[];
    const ordered = FIELD_ORDER.filter((k) => k in data).map((k) => ({
      key: k,
      label: FIELD_LABELS[k]?.label ?? humanise(k),
      value: data[k],
    }));
    const extras = Object.keys(data)
      .filter((k) => !FIELD_ORDER.includes(k) && !(k in FIELD_LABELS))
      .map((k) => ({ key: k, label: humanise(k), value: data[k] }));
    return [...ordered, ...extras];
  }, [data]);

  return (
    <div className="rounded-2xl border border-border bg-surface p-6 shadow-lg">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-text-primary">
            Diagnostics
          </h2>
          <p className="mt-1 text-xs text-text-secondary">
            Environment checks the daemon performs on demand. Share these with
            support if something is not working.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button
            type="button"
            onClick={refetch}
            disabled={isLoading}
            className="rounded-lg border border-border bg-surface px-3 py-1.5 text-xs text-text-primary transition-colors hover:bg-sidebar-hover disabled:opacity-50"
          >
            {isLoading ? "Refreshing" : "Refresh"}
          </button>
          <button
            type="button"
            onClick={handleCopy}
            disabled={!data}
            className="rounded-lg bg-accent px-3 py-1.5 text-xs text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
          >
            {copyState === "copied"
              ? "Copied"
              : copyState === "error"
                ? "Copy failed"
                : "Copy summary"}
          </button>
        </div>
      </div>

      {error && (
        <p className="mt-4 rounded-lg bg-status-error/10 px-3 py-2 text-xs text-status-error">
          Could not load diagnostics: {error}
        </p>
      )}

      {isLoading && !data && (
        <p className="mt-4 text-xs text-text-secondary">Running checks...</p>
      )}

      {lastPipelineError && (
        <div
          role="alert"
          className="mt-4 rounded-lg border border-status-error/40 bg-status-error/10 p-3"
        >
          <p className="text-xs font-medium text-status-error">
            Last pipeline error
          </p>
          <p className="mt-1 text-xs text-text-primary">{lastPipelineError}</p>
          {recoveryHint && (
            <p className="mt-2 text-xs text-text-secondary">
              <span className="font-medium">Suggested fix:</span> {recoveryHint}
            </p>
          )}
        </div>
      )}

      {data && (
        <ul className="mt-4 divide-y divide-border">
          {rows.map((row) => (
            <li
              key={row.key}
              className="flex items-center justify-between gap-4 py-2"
            >
              <span className="text-sm text-text-primary">{row.label}</span>
              <FieldValue name={row.key} value={row.value} />
            </li>
          ))}
        </ul>
      )}

      {!preflightNotImplemented && (preflight || preflightLoading) && (
        <PreflightSection
          data={preflight}
          isLoading={preflightLoading && !preflight}
        />
      )}
    </div>
  );
}

function PreflightSection({
  data,
  isLoading,
}: {
  data: PreflightData | null;
  isLoading: boolean;
}) {
  return (
    <div className="mt-6 border-t border-border pt-4">
      <h3 className="text-sm font-semibold text-text-primary">Preflight</h3>
      <p className="mt-1 text-xs text-text-secondary">
        Pre-recording readiness checks.
      </p>
      {isLoading && (
        <p className="mt-2 text-xs text-text-secondary">Running preflight...</p>
      )}
      {data?.checks && data.checks.length > 0 && (
        <ul className="mt-3 divide-y divide-border">
          {data.checks.map((c, i) => (
            <li
              key={`${c.name}-${i}`}
              className="flex items-start justify-between gap-4 py-2"
            >
              <div className="min-w-0">
                <p className="text-sm text-text-primary">{c.name}</p>
                {c.message && (
                  <p className="mt-0.5 text-xs text-text-secondary">
                    {c.message}
                  </p>
                )}
              </div>
              <StatusPill
                tone={
                  c.status === "pass"
                    ? "ok"
                    : c.status === "fail"
                      ? "fail"
                      : "info"
                }
              >
                {c.status.toUpperCase()}
              </StatusPill>
            </li>
          ))}
        </ul>
      )}
      {data && !data.checks?.length && !isLoading && (
        <p className="mt-2 text-xs text-text-secondary">No checks reported.</p>
      )}
    </div>
  );
}
