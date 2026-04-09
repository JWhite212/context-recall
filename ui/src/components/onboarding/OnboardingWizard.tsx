import { useState } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { getDevices, getModels, downloadModel, getConfig } from "../../lib/api";
import { Spinner } from "../common/Spinner";
import type { WhisperModel } from "../../lib/types";

const STORAGE_KEY = "meetingmind-onboarding-complete";

export function isOnboardingComplete(): boolean {
  return localStorage.getItem(STORAGE_KEY) === "true";
}

export function markOnboardingComplete(): void {
  localStorage.setItem(STORAGE_KEY, "true");
}

interface OnboardingWizardProps {
  onComplete: () => void;
}

const STEPS = [
  "Welcome",
  "Audio Setup",
  "Transcription",
  "Summarisation",
  "Output",
  "Done",
] as const;

export function OnboardingWizard({ onComplete }: OnboardingWizardProps) {
  const [stepIdx, setStepIdx] = useState(0);
  const step = STEPS[stepIdx];

  const next = () => {
    if (stepIdx < STEPS.length - 1) setStepIdx(stepIdx + 1);
  };
  const prev = () => {
    if (stepIdx > 0) setStepIdx(stepIdx - 1);
  };

  const finish = () => {
    markOnboardingComplete();
    onComplete();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-surface">
      <div className="w-full max-w-xl mx-auto px-8">
        {/* Progress */}
        <div className="flex items-center gap-1 mb-8">
          {STEPS.map((s, i) => (
            <div
              key={s}
              className={`h-1 flex-1 rounded-full transition-colors ${
                i <= stepIdx ? "bg-accent" : "bg-border"
              }`}
            />
          ))}
        </div>

        {/* Step content */}
        <div className="min-h-[320px]">
          {step === "Welcome" && <WelcomeStep />}
          {step === "Audio Setup" && <AudioStep />}
          {step === "Transcription" && <TranscriptionStep />}
          {step === "Summarisation" && <SummarisationStep />}
          {step === "Output" && <OutputStep />}
          {step === "Done" && <DoneStep />}
        </div>

        {/* Navigation */}
        <div className="flex items-center justify-between mt-8">
          <button
            onClick={prev}
            disabled={stepIdx === 0}
            className="px-4 py-2 text-sm text-text-secondary hover:text-text-primary disabled:opacity-0 transition-colors"
          >
            Back
          </button>
          {step === "Done" ? (
            <button
              onClick={finish}
              className="px-6 py-2 text-sm font-medium rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
            >
              Start MeetingMind
            </button>
          ) : (
            <button
              onClick={next}
              className="px-6 py-2 text-sm font-medium rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
            >
              Continue
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Step components                                                    */
/* ------------------------------------------------------------------ */

function WelcomeStep() {
  return (
    <div className="text-center">
      <div className="text-4xl mb-4">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-accent mx-auto">
          <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
          <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
          <line x1="12" y1="19" x2="12" y2="23" />
          <line x1="8" y1="23" x2="16" y2="23" />
        </svg>
      </div>
      <h1 className="text-2xl font-semibold text-text-primary mb-3">
        Welcome to MeetingMind
      </h1>
      <p className="text-sm text-text-secondary max-w-md mx-auto leading-relaxed">
        MeetingMind automatically detects your Teams meetings, records audio,
        transcribes locally using Whisper, and produces AI-powered summaries.
      </p>
      <p className="text-xs text-text-muted mt-4 max-w-sm mx-auto">
        All processing happens on your machine. No audio leaves your computer.
      </p>
    </div>
  );
}

function AudioStep() {
  const { data, isLoading } = useQuery({
    queryKey: ["devices"],
    queryFn: getDevices,
  });

  const devices = data?.devices ?? [];
  const hasBlackHole = devices.some((d) =>
    d.name.toLowerCase().includes("blackhole"),
  );

  return (
    <div>
      <h2 className="text-xl font-semibold text-text-primary mb-2">
        Audio Setup
      </h2>
      <p className="text-sm text-text-secondary mb-6">
        MeetingMind uses BlackHole to capture system audio. Check that it's installed and visible.
      </p>

      {isLoading ? (
        <div className="flex items-center gap-2 py-4">
          <Spinner />
          <span className="text-sm text-text-muted">Checking audio devices...</span>
        </div>
      ) : (
        <>
          <div className="flex items-center gap-3 mb-4">
            <span
              className={`w-3 h-3 rounded-full ${
                hasBlackHole ? "bg-status-idle" : "bg-status-error"
              }`}
            />
            <span className="text-sm text-text-primary">
              {hasBlackHole
                ? "BlackHole detected"
                : "BlackHole not found"}
            </span>
          </div>

          {!hasBlackHole && (
            <div className="rounded-lg bg-status-error/5 border border-status-error/20 p-4 mb-4">
              <p className="text-xs text-text-secondary">
                Install BlackHole from{" "}
                <span className="font-mono text-accent">existential.audio/blackhole</span>,
                then set up a Multi-Output Device in Audio MIDI Setup.
              </p>
            </div>
          )}

          <div className="rounded-lg bg-surface border border-border p-3 max-h-40 overflow-y-auto">
            <p className="text-xs text-text-muted mb-2">Detected audio devices:</p>
            {devices.map((d) => (
              <div key={d.index} className="text-xs text-text-secondary py-0.5">
                {d.name}
                {d.is_default && (
                  <span className="ml-1 text-accent">(default)</span>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function TranscriptionStep() {
  const { data, isLoading } = useQuery({
    queryKey: ["models"],
    queryFn: getModels,
    refetchInterval: 3000,
  });

  const dl = useMutation({
    mutationFn: (name: string) => downloadModel(name),
  });

  const models = data?.models ?? [];

  return (
    <div>
      <h2 className="text-xl font-semibold text-text-primary mb-2">
        Transcription Model
      </h2>
      <p className="text-sm text-text-secondary mb-6">
        Download a Whisper model for local speech-to-text. Larger models are more accurate but slower.
      </p>

      {isLoading ? (
        <div className="flex items-center gap-2 py-4">
          <Spinner />
          <span className="text-sm text-text-muted">Loading models...</span>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {models.map((m: WhisperModel) => (
            <div
              key={m.name}
              className="flex items-center justify-between py-2 px-3 rounded-lg bg-surface border border-border"
            >
              <div>
                <span className="text-sm text-text-primary">{m.name}</span>
                <span className="text-xs text-text-muted ml-2">{m.size_mb} MB</span>
              </div>
              {m.status === "downloaded" ? (
                <span className="text-xs text-status-idle">Ready</span>
              ) : m.status === "downloading" ? (
                <span className="text-xs text-blue-400 flex items-center gap-1">
                  <Spinner className="h-3 w-3" /> Downloading...
                </span>
              ) : (
                <button
                  onClick={() => dl.mutate(m.name)}
                  className="text-xs px-3 py-1 rounded-lg bg-accent text-white hover:bg-accent-hover transition-colors"
                >
                  Download
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SummarisationStep() {
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });

  const backend = config?.summarisation?.backend ?? "ollama";

  return (
    <div>
      <h2 className="text-xl font-semibold text-text-primary mb-2">
        Summarisation
      </h2>
      <p className="text-sm text-text-secondary mb-6">
        Choose how meeting summaries are generated. You can change this later in Settings.
      </p>

      <div className="flex flex-col gap-3">
        <div
          className={`p-4 rounded-lg border transition-colors ${
            backend === "ollama"
              ? "border-accent bg-accent/5"
              : "border-border bg-surface"
          }`}
        >
          <div className="text-sm font-medium text-text-primary">Ollama (Local)</div>
          <p className="text-xs text-text-muted mt-1">
            Free, runs on your machine. Requires Ollama installed with a model like llama3.1.
          </p>
        </div>

        <div
          className={`p-4 rounded-lg border transition-colors ${
            backend === "claude"
              ? "border-accent bg-accent/5"
              : "border-border bg-surface"
          }`}
        >
          <div className="text-sm font-medium text-text-primary">Claude (API)</div>
          <p className="text-xs text-text-muted mt-1">
            Higher quality summaries via the Anthropic API. Requires an API key.
          </p>
        </div>
      </div>

      <p className="text-xs text-text-muted mt-4">
        Current backend: <span className="font-medium text-text-secondary">{backend}</span>.
        Change it in Settings after setup.
      </p>
    </div>
  );
}

function OutputStep() {
  const { data: config } = useQuery({
    queryKey: ["config"],
    queryFn: getConfig,
  });

  return (
    <div>
      <h2 className="text-xl font-semibold text-text-primary mb-2">
        Output
      </h2>
      <p className="text-sm text-text-secondary mb-6">
        Summaries can be written to an Obsidian vault and/or Notion. Configure these in Settings.
      </p>

      <div className="flex flex-col gap-3">
        <div className="flex items-center gap-3 p-3 rounded-lg bg-surface border border-border">
          <span
            className={`w-2.5 h-2.5 rounded-full ${
              config?.markdown?.enabled ? "bg-status-idle" : "bg-border"
            }`}
          />
          <div>
            <div className="text-sm text-text-primary">Markdown</div>
            <div className="text-xs text-text-muted">
              {config?.markdown?.enabled
                ? `Writing to ${config.markdown.vault_path}`
                : "Disabled"}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-3 p-3 rounded-lg bg-surface border border-border">
          <span
            className={`w-2.5 h-2.5 rounded-full ${
              config?.notion?.enabled ? "bg-status-idle" : "bg-border"
            }`}
          />
          <div>
            <div className="text-sm text-text-primary">Notion</div>
            <div className="text-xs text-text-muted">
              {config?.notion?.enabled ? "Connected" : "Disabled"}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function DoneStep() {
  return (
    <div className="text-center">
      <div className="mb-4">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" className="text-status-idle mx-auto">
          <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
          <polyline points="22 4 12 14.01 9 11.01" />
        </svg>
      </div>
      <h2 className="text-2xl font-semibold text-text-primary mb-3">
        You're all set
      </h2>
      <p className="text-sm text-text-secondary max-w-sm mx-auto">
        MeetingMind will automatically detect and transcribe your Teams meetings.
        You can fine-tune everything in Settings.
      </p>
    </div>
  );
}
