import { useState, useEffect, useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { startRecording, stopRecording } from "../../lib/api";
import { useDaemonStatus } from "../../hooks/useDaemonStatus";
import { useAppStore } from "../../stores/appStore";
import { useToast } from "../common/Toast";

/* ------------------------------------------------------------------ */
/*  Pipeline stage labels                                             */
/* ------------------------------------------------------------------ */

const PIPELINE_STAGES = [
  { key: "recording", label: "Recording" },
  { key: "transcribing", label: "Transcribing" },
  { key: "diarising", label: "Diarising" },
  { key: "summarising", label: "Summarising" },
  { key: "writing", label: "Writing" },
] as const;

function stageIndex(stage: string | null): number {
  if (!stage) return -1;
  return PIPELINE_STAGES.findIndex((s) => s.key === stage);
}

/* ------------------------------------------------------------------ */
/*  Elapsed timer                                                      */
/* ------------------------------------------------------------------ */

function formatElapsed(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0)
    return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function useElapsedTimer(startedAt: number | null): number {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!startedAt) {
      setElapsed(0);
      return;
    }
    setElapsed(Math.max(0, Date.now() / 1000 - startedAt));
    const interval = setInterval(() => {
      setElapsed(Math.max(0, Date.now() / 1000 - startedAt));
    }, 1000);
    return () => clearInterval(interval);
  }, [startedAt]);

  return elapsed;
}

/* ------------------------------------------------------------------ */
/*  Main component                                                    */
/* ------------------------------------------------------------------ */

export function LiveView() {
  const { daemonRunning, state, activeMeeting } = useDaemonStatus();
  const pipelineStage = useAppStore((s) => s.pipelineStage);
  const liveSegments = useAppStore((s) => s.liveSegments);
  const audioLevels = useAppStore((s) => s.audioLevels);
  const queryClient = useQueryClient();
  const segmentsEndRef = useRef<HTMLDivElement>(null);

  const isRecording = state === "recording";
  const isProcessing = pipelineStage !== null && !isRecording;

  const elapsed = useElapsedTimer(
    isRecording ? (activeMeeting?.started_at ?? null) : null,
  );

  // Determine which stage to highlight.
  const currentStageKey = isRecording ? "recording" : pipelineStage;
  const activeIdx = stageIndex(currentStageKey);

  // Auto-scroll transcript.
  useEffect(() => {
    segmentsEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [liveSegments.length]);

  const toast = useToast();

  const startMutation = useMutation({
    mutationFn: startRecording,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["status"] });
      toast.success("Recording started.");
    },
  });

  const [showStopDialog, setShowStopDialog] = useState(false);

  const stopMutation = useMutation({
    mutationFn: (defer: boolean) => stopRecording(defer),
    onSuccess: (_data, defer) => {
      queryClient.invalidateQueries({ queryKey: ["status"] });
      setShowStopDialog(false);
      if (defer) {
        toast.info("Recording saved. Process it later from Meetings.");
      } else {
        toast.info("Recording stopped. Processing will begin shortly.");
      }
    },
    onError: () => {
      setShowStopDialog(false);
    },
  });

  return (
    <div className="flex flex-col gap-6 p-6 max-w-3xl">
      <h1 className="text-lg font-semibold text-text-primary">Live</h1>

      {/* Status + timer */}
      <div className="rounded-xl bg-surface-raised border border-border p-6 flex flex-col items-center gap-4">
        {/* Recording indicator */}
        <div className="flex items-center gap-3">
          {isRecording && (
            <span className="w-3 h-3 rounded-full bg-status-recording animate-pulse" />
          )}
          <span className="text-sm font-medium text-text-secondary">
            {!daemonRunning
              ? "Daemon offline"
              : isRecording
                ? "Recording"
                : isProcessing
                  ? "Processing"
                  : "Idle"}
          </span>
        </div>

        {/* Elapsed time */}
        <div className="text-5xl font-light text-text-primary font-mono tabular-nums tracking-tight">
          {formatElapsed(elapsed)}
        </div>

        {/* Start/Stop button */}
        {daemonRunning && !showStopDialog && (
          <button
            onClick={() =>
              isRecording ? setShowStopDialog(true) : startMutation.mutate()
            }
            disabled={
              startMutation.isPending || stopMutation.isPending || isProcessing
            }
            aria-label={isRecording ? "Stop recording" : "Start recording"}
            className={`mt-2 px-6 py-2 rounded-full text-sm font-medium transition-colors ${
              isRecording
                ? "bg-status-error text-white hover:opacity-90"
                : isProcessing
                  ? "bg-surface text-text-muted border border-border cursor-not-allowed"
                  : "bg-accent text-white hover:bg-accent-hover"
            } disabled:opacity-50`}
          >
            {startMutation.isPending
              ? "Starting..."
              : stopMutation.isPending
                ? "Stopping..."
                : isRecording
                  ? "Stop Recording"
                  : isProcessing
                    ? "Processing..."
                    : "Start Recording"}
          </button>
        )}

        {/* Stop confirmation dialog */}
        {showStopDialog && (
          <div className="mt-2 flex flex-col items-center gap-3">
            <p className="text-sm text-text-secondary">Stop recording and...</p>
            <div className="flex gap-2">
              <button
                onClick={() => stopMutation.mutate(false)}
                disabled={stopMutation.isPending}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50 transition-colors"
              >
                {stopMutation.isPending ? "Stopping..." : "Process Now"}
              </button>
              <button
                onClick={() => stopMutation.mutate(true)}
                disabled={stopMutation.isPending}
                className="px-4 py-2 rounded-lg text-sm font-medium bg-surface-raised text-text-primary border border-border hover:bg-surface transition-colors disabled:opacity-50"
              >
                Process Later
              </button>
            </div>
            <button
              onClick={() => setShowStopDialog(false)}
              className="text-xs text-text-muted hover:text-text-secondary transition-colors"
            >
              Cancel
            </button>
          </div>
        )}

        {(startMutation.isError || stopMutation.isError) && (
          <p className="text-xs text-status-error mt-1">
            {((startMutation.error || stopMutation.error) as Error)?.message}
          </p>
        )}
      </div>

      {/* Audio level meters */}
      {isRecording && (
        <div className="rounded-xl bg-surface-raised border border-border p-5">
          <h2 className="text-sm font-medium text-text-primary mb-3">
            Audio Levels
          </h2>
          <div className="flex flex-col gap-3">
            <LevelMeter
              label="System"
              value={audioLevels.system}
              color="bg-status-idle"
            />
            <LevelMeter label="Mic" value={audioLevels.mic} color="bg-accent" />
          </div>
        </div>
      )}

      {/* Pipeline progress */}
      {(isRecording || isProcessing) && (
        <div
          className="rounded-xl bg-surface-raised border border-border p-5"
          aria-busy={isProcessing}
        >
          <h2 className="text-sm font-medium text-text-primary mb-4">
            Pipeline
          </h2>
          <div className="flex items-center gap-1">
            {PIPELINE_STAGES.map((stage, i) => {
              const isActive = i === activeIdx;
              const isDone = activeIdx > i;
              return (
                <div key={stage.key} className="flex items-center gap-1 flex-1">
                  {/* Step indicator */}
                  <div className="flex flex-col items-center gap-1.5 flex-1">
                    <div
                      className={`h-1.5 w-full rounded-full transition-colors ${
                        isDone
                          ? "bg-status-idle"
                          : isActive
                            ? "bg-accent animate-pulse"
                            : "bg-border"
                      }`}
                    />
                    <span
                      className={`text-[10px] ${
                        isActive
                          ? "text-accent font-medium"
                          : isDone
                            ? "text-status-idle"
                            : "text-text-muted"
                      }`}
                    >
                      {stage.label}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Live transcript */}
      {liveSegments.length > 0 && (
        <div className="rounded-xl bg-surface-raised border border-border p-5">
          <h2 className="text-sm font-medium text-text-primary mb-3">
            Live Transcript
          </h2>
          <div
            className="max-h-80 overflow-y-auto space-y-2"
            aria-live="polite"
          >
            {liveSegments.map((seg, i) => (
              <div key={i} className="flex gap-3 text-sm">
                <span className="text-text-muted font-mono text-xs pt-0.5 shrink-0 w-12 text-right">
                  {formatTimestamp(seg.start)}
                </span>
                {seg.speaker && (
                  <span
                    className={`text-xs font-medium pt-0.5 shrink-0 w-16 ${
                      seg.speaker === "Me" ? "text-accent" : "text-status-idle"
                    }`}
                  >
                    {seg.speaker}
                  </span>
                )}
                <span className="text-text-primary">{seg.text}</span>
              </div>
            ))}
            <div ref={segmentsEndRef} />
          </div>
        </div>
      )}

      {/* Empty state */}
      {!isRecording &&
        !isProcessing &&
        liveSegments.length === 0 &&
        daemonRunning && (
          <div className="rounded-xl bg-surface-raised border border-border p-8 flex flex-col items-center gap-3">
            <svg
              width="40"
              height="40"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
              className="text-text-muted"
            >
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
            <p className="text-sm text-text-muted text-center">
              Start a manual recording or wait for a meeting to be
              auto-detected.
            </p>
          </div>
        )}
    </div>
  );
}

function LevelMeter({
  label,
  value,
  color,
}: {
  label: string;
  value: number;
  color: string;
}) {
  // RMS values are typically 0–0.3 for speech; scale to fill the bar.
  const pct = Math.min(100, Math.round(value * 400));
  return (
    <div className="flex items-center gap-3">
      <span className="text-xs text-text-muted w-12 text-right shrink-0">
        {label}
      </span>
      <div
        className="flex-1 h-2 rounded-full bg-border overflow-hidden"
        role="progressbar"
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label} audio level`}
      >
        <div
          className={`h-full rounded-full transition-[width] duration-100 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10px] text-text-muted w-8 font-mono tabular-nums">
        {pct > 0 ? `${Math.round(20 * Math.log10(value + 1e-10))}` : "\u2014"}
      </span>
    </div>
  );
}

function formatTimestamp(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
