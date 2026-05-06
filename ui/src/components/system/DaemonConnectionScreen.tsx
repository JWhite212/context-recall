import { useState } from "react";
import type { DaemonConnectionState } from "../../hooks/useDaemonConnection";
import { DiagnosticsPanel } from "./DiagnosticsPanel";

interface Props {
  state: DaemonConnectionState;
  error: string | null;
  onRetry: () => void;
  onStartLocal: () => void;
  onOpenLogs: () => void;
  onOpenAppSupport: () => void;
}

interface MessageProps {
  title: string;
  body: string;
  primaryLabel: string;
  primaryDisabled?: boolean;
  primaryAction: () => void;
  showStartLocal?: boolean;
}

const MESSAGES: Record<
  Exclude<DaemonConnectionState, "connected">,
  Omit<MessageProps, "primaryAction" | "primaryLabel">
> = {
  checking: {
    title: "Connecting to local service",
    body: "Verifying that the Context Recall daemon is reachable.",
  },
  starting: {
    title: "Starting local service",
    body: "Launching the bundled daemon and waiting for it to come online.",
    primaryDisabled: true,
  },
  "missing-token": {
    title: "Authentication token not found",
    body: "The daemon is running but no auth token is on disk yet. This usually clears within a few seconds of the daemon starting; if it persists, restart the service.",
  },
  unauthorised: {
    title: "Authentication failed",
    body: "The auth token on disk did not match the daemon's expected value. Restart the daemon to regenerate the token.",
  },
  unavailable: {
    title: "Local service is not running",
    body: "Context Recall couldn't reach the daemon at 127.0.0.1:9876. Start the local service to continue, or check that nothing else is occupying the port.",
    showStartLocal: true,
  },
  failed: {
    title: "Connection failed",
    body: "Something went wrong while talking to the daemon. The error below may help diagnose the problem.",
  },
};

export function DaemonConnectionScreen({
  state,
  error,
  onRetry,
  onStartLocal,
  onOpenLogs,
  onOpenAppSupport,
}: Props) {
  const [showDiagnostics, setShowDiagnostics] = useState(false);

  if (state === "connected") return null;

  const msg = MESSAGES[state];
  const showStartLocal = !!msg.showStartLocal;

  return (
    <div className="flex h-screen flex-col items-center overflow-y-auto bg-background p-8">
      <div className="w-full max-w-lg rounded-2xl border border-border bg-surface p-8 shadow-lg">
        <h1 className="text-lg font-semibold text-text-primary">{msg.title}</h1>
        <p className="mt-2 text-sm text-text-secondary">{msg.body}</p>
        {error && (
          <pre className="mt-4 max-h-32 overflow-auto rounded-lg bg-background p-3 text-xs text-status-error">
            {error}
          </pre>
        )}
        <div className="mt-6 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={onRetry}
            disabled={state === "checking" || state === "starting"}
            className="rounded-lg bg-accent px-4 py-2 text-sm text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
          >
            Retry connection
          </button>
          {showStartLocal && (
            <button
              type="button"
              onClick={onStartLocal}
              disabled={state === "starting"}
              className="rounded-lg border border-border bg-surface px-4 py-2 text-sm text-text-primary transition-colors hover:bg-sidebar-hover disabled:opacity-50"
            >
              Start local service
            </button>
          )}
          <button
            type="button"
            onClick={onOpenLogs}
            className="rounded-lg border border-border bg-surface px-4 py-2 text-sm text-text-primary transition-colors hover:bg-sidebar-hover"
          >
            Open logs
          </button>
          <button
            type="button"
            onClick={onOpenAppSupport}
            className="rounded-lg border border-border bg-surface px-4 py-2 text-sm text-text-primary transition-colors hover:bg-sidebar-hover"
          >
            Open data folder
          </button>
          <button
            type="button"
            onClick={() => setShowDiagnostics((v) => !v)}
            aria-expanded={showDiagnostics}
            className="rounded-lg border border-border bg-surface px-4 py-2 text-sm text-text-primary transition-colors hover:bg-sidebar-hover"
          >
            {showDiagnostics ? "Hide diagnostics" : "Open diagnostics"}
          </button>
        </div>
      </div>
      {showDiagnostics && (
        <div className="mt-6 w-full max-w-lg">
          <DiagnosticsPanel />
        </div>
      )}
    </div>
  );
}
