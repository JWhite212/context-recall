import { useEffect, useRef, useState, useCallback, type MutableRefObject } from "react";
import WaveSurfer from "wavesurfer.js";

const PLAYBACK_RATES = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;
const SKIP_SECONDS = 10;

export interface AudioSeekHandle {
  seekTo: (seconds: number) => void;
}

interface AudioPlayerProps {
  /** Full URL to the audio file. */
  src: string;
  /** Optional ref that gets populated with seek controls. */
  seekRef?: MutableRefObject<AudioSeekHandle | null>;
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export function AudioPlayer({ src, seekRef }: AudioPlayerProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);

  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [rate, setRate] = useState(1);
  const [loading, setLoading] = useState(true);

  // Initialise WaveSurfer.
  useEffect(() => {
    if (!containerRef.current) return;

    const ws = WaveSurfer.create({
      container: containerRef.current,
      height: 64,
      waveColor: "rgba(255, 255, 255, 0.2)",
      progressColor: "rgba(99, 102, 241, 0.7)",
      cursorColor: "rgba(99, 102, 241, 0.9)",
      cursorWidth: 1,
      barWidth: 2,
      barGap: 1,
      barRadius: 2,
      normalize: true,
      url: src,
    });

    ws.on("ready", () => {
      setDuration(ws.getDuration());
      setLoading(false);
      if (seekRef) {
        seekRef.current = {
          seekTo: (seconds: number) => {
            const dur = ws.getDuration();
            if (dur > 0) {
              ws.seekTo(Math.max(0, Math.min(1, seconds / dur)));
              ws.play();
            }
          },
        };
      }
    });
    ws.on("timeupdate", (time) => setCurrentTime(time));
    ws.on("play", () => setPlaying(true));
    ws.on("pause", () => setPlaying(false));
    ws.on("finish", () => setPlaying(false));

    wsRef.current = ws;

    return () => {
      ws.destroy();
      wsRef.current = null;
    };
  }, [src]);

  const togglePlay = useCallback(() => {
    wsRef.current?.playPause();
  }, []);

  const skip = useCallback((delta: number) => {
    const ws = wsRef.current;
    if (!ws) return;
    const target = Math.max(0, Math.min(ws.getDuration(), ws.getCurrentTime() + delta));
    ws.seekTo(target / ws.getDuration());
  }, []);

  const cycleRate = useCallback(() => {
    setRate((prev) => {
      const idx = PLAYBACK_RATES.indexOf(prev as (typeof PLAYBACK_RATES)[number]);
      const next = PLAYBACK_RATES[(idx + 1) % PLAYBACK_RATES.length];
      wsRef.current?.setPlaybackRate(next);
      return next;
    });
  }, []);

  return (
    <div className="rounded-xl bg-surface-raised border border-border p-4 flex flex-col gap-3">
      {/* Waveform */}
      <div ref={containerRef} className={loading ? "opacity-30" : ""} />

      {/* Controls */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          {/* Skip back */}
          <button
            onClick={() => skip(-SKIP_SECONDS)}
            className="p-1.5 rounded-md text-text-secondary hover:text-text-primary hover:bg-sidebar-hover transition-colors"
            title={`Back ${SKIP_SECONDS}s`}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="11 19 2 12 11 5 11 19" />
              <polygon points="22 19 13 12 22 5 22 19" />
            </svg>
          </button>

          {/* Play/Pause */}
          <button
            onClick={togglePlay}
            disabled={loading}
            className="p-2 rounded-full bg-accent text-white hover:bg-accent-hover transition-colors disabled:opacity-50"
          >
            {playing ? (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="4" width="4" height="16" />
                <rect x="14" y="4" width="4" height="16" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <polygon points="5 3 19 12 5 21 5 3" />
              </svg>
            )}
          </button>

          {/* Skip forward */}
          <button
            onClick={() => skip(SKIP_SECONDS)}
            className="p-1.5 rounded-md text-text-secondary hover:text-text-primary hover:bg-sidebar-hover transition-colors"
            title={`Forward ${SKIP_SECONDS}s`}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="13 19 22 12 13 5 13 19" />
              <polygon points="2 19 11 12 2 5 2 19" />
            </svg>
          </button>
        </div>

        {/* Time display */}
        <span className="text-xs text-text-muted font-mono tabular-nums">
          {formatTime(currentTime)} / {formatTime(duration)}
        </span>

        {/* Playback speed */}
        <button
          onClick={cycleRate}
          className="px-2 py-0.5 rounded-md text-xs font-medium text-text-secondary hover:text-text-primary hover:bg-sidebar-hover transition-colors"
          title="Playback speed"
        >
          {rate}x
        </button>
      </div>
    </div>
  );
}
