import { create } from "zustand";
import type { WSEvent, TranscriptSegment } from "../lib/types";

interface AudioLevels {
  system: number;
  mic: number;
}

interface ModelProgress {
  percent: number;
  error?: string;
}

interface PipelineWarning {
  source: string;
  message: string;
}

interface AppState {
  /** WebSocket connection status. */
  wsConnected: boolean;
  setWsConnected: (connected: boolean) => void;

  /** Current pipeline stage for the active meeting. */
  pipelineStage: string | null;

  /** Latest non-fatal pipeline warning (e.g. silent system audio). One
   *  per active recording session; cleared on session boundaries. */
  pipelineWarning: PipelineWarning | null;

  /** Live transcript segments for the active meeting. */
  liveSegments: TranscriptSegment[];

  /** Live audio levels (RMS, 0.0–1.0). */
  audioLevels: AudioLevels;

  /** Model download progress from WebSocket events. */
  modelProgress: Record<string, ModelProgress>;

  /** Unread notification count. */
  unreadNotifications: number;
  incrementNotifications: () => void;
  setUnreadNotifications: (count: number) => void;

  /** Handle a WebSocket event. */
  handleEvent: (event: WSEvent) => void;

  /** Reset live state (e.g., when a meeting completes). */
  resetLive: () => void;
}

export const useAppStore = create<AppState>((set) => ({
  wsConnected: false,
  setWsConnected: (connected) => set({ wsConnected: connected }),

  pipelineStage: null,
  pipelineWarning: null,
  liveSegments: [],
  audioLevels: { system: 0, mic: 0 },
  modelProgress: {},

  unreadNotifications: 0,
  incrementNotifications: () =>
    set((state) => ({ unreadNotifications: state.unreadNotifications + 1 })),
  setUnreadNotifications: (count) => set({ unreadNotifications: count }),

  handleEvent: (event) => {
    switch (event.type) {
      case "meeting.started":
        // New session — clear any warning left over from a previous run.
        set({ pipelineWarning: null });
        break;
      case "pipeline.stage":
        set({ pipelineStage: event.stage });
        break;
      case "pipeline.warning":
        set({
          pipelineWarning: {
            source: event.source,
            message: event.message,
          },
        });
        break;
      case "pipeline.complete":
        set({
          pipelineStage: null,
          pipelineWarning: null,
          liveSegments: [],
          audioLevels: { system: 0, mic: 0 },
        });
        break;
      case "pipeline.error":
        set({
          pipelineStage: null,
          pipelineWarning: null,
          audioLevels: { system: 0, mic: 0 },
        });
        break;
      case "transcript.segment":
        if (!event.segment?.text?.trim()) break;
        set((state) => {
          const segments = [...state.liveSegments, event.segment];
          return {
            liveSegments:
              segments.length > 200 ? segments.slice(-200) : segments,
          };
        });
        break;
      case "audio.level":
        set({
          audioLevels: {
            system: event.system_rms ?? 0,
            mic: event.mic_rms ?? 0,
          },
        });
        break;
      case "model.download.progress":
        set((state) => ({
          modelProgress: {
            ...state.modelProgress,
            [event.model]: { percent: event.percent, error: event.error },
          },
        }));
        break;
      case "notification":
        set((state) => ({
          unreadNotifications: state.unreadNotifications + 1,
        }));
        break;
    }
  },

  resetLive: () =>
    set({
      pipelineStage: null,
      pipelineWarning: null,
      liveSegments: [],
      audioLevels: { system: 0, mic: 0 },
    }),
}));
