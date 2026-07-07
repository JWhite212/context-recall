# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

### Python daemon + API server

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml  # then edit with real values

# Three run modes (all start the FastAPI server in-process when api.enabled=true)
python3 -m src.main                    # Daemon: auto-detect Teams meetings
python3 -m src.main --record-now       # Manual: record immediately, Ctrl+C to stop
python3 -m src.main --process file.wav # Process an existing audio file
python3 -m src.main --config path.yaml # Use a non-default config

# Verify imports
python3 -c "from src.main import ContextRecall"
```

### Tauri desktop app (the user-facing UI)

```bash
cd ui
npm install
npm run tauri dev    # Tauri shell + Vite dev server + spawns the Python daemon
npm run dev          # Vite-only (no native shell, no daemon — for UI iteration)
npm run build        # tsc + vite build (production bundle)
```

The Tauri bundle ships a PyInstaller-built daemon as a sidecar at `ui/src-tauri/resources/context-recall-daemon/`. Locally that directory is empty (only `.gitkeep`); CI and `npm run tauri build` populate it. If `cargo check` complains about the missing path during a fresh clone, `mkdir -p ui/src-tauri/resources/context-recall-daemon && touch ui/src-tauri/resources/context-recall-daemon/.gitkeep` is the same stub CI uses.

### Tests and lint

```bash
# Python
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v            # Full Python suite (~690 tests)
python3 -m pytest tests/ -x            # Stop on first failure
ruff check src/ tests/                 # Lint check

# UI
cd ui
npm test                               # vitest run (~20 tests)
npx tsc --noEmit                       # TypeScript type check

# Rust (Tauri shell)
cd ui/src-tauri && cargo check
```

### Profile-aware data paths

`src/utils/paths.py` selects a data directory from `CONTEXT_RECALL_PROFILE` (`dev` | `prod` | `test`). Dev and prod use **separate SQLite databases and audio directories** so a `npm run tauri dev` session cannot touch production meetings. `CONTEXT_RECALL_DATA_DIR` overrides regardless of profile.

## Architecture

Context Recall is a macOS-only system with two cooperating processes:

1. **Python daemon** (`src/main.py`) — pipeline orchestrator + embedded FastAPI server.
2. **Tauri desktop app** (`ui/src-tauri/` + `ui/src/`) — native macOS shell wrapping a React UI that talks to the daemon over HTTP + WebSocket.

The recording pipeline is sequential, but live transcription runs in parallel with capture so the UI can stream segments while the meeting is still going:

```
TeamsDetector  ──►  AudioCapture  ──►  Transcriber  ──►  Diariser  ──►  Summariser  ──►  Writers + DB + Intelligence
                         │
                         └──►  LiveTranscriber (parallel chunks → WS events)
```

### Pipeline core

**`src/main.py`** — Orchestrator (`ContextRecall` class). Wires components together, manages lifecycle, owns the embedded `ApiServer` reference. Detector callbacks fire `_on_meeting_start` / `_on_meeting_end`; the latter dispatches the live-transcriber join to a daemon thread (so the detector callback returns promptly — see X4 fix in `654ecff`) and submits `_process_audio` to a `ThreadPoolExecutor`. `_process_audio` runs transcribe → diarise → summarise → DB persist → write outputs → post-process.

**`src/detector.py`** — State machine (IDLE → ACTIVE → ENDING) with debounce. Delegates platform-specific detection (`pgrep`, `lsof`, `osascript`) to `src/platform/` implementations via the `PlatformDetector` protocol.

**`src/platform/`** — Platform abstraction layer. `PlatformDetector` protocol with `MacOSDetector` (the only implementation that actually works), plus Linux/Windows stubs.

**`src/audio_capture.py`** — Records BlackHole (system audio) and microphone to **separate WAV files** on independent `sd.InputStream` threads, then post-merges with RMS normalisation. This avoids clock-drift between the two devices. Source files can be kept for diarisation. `start()` exposes `last_warning` (e.g. mic fallback) and `last_error` (typed `AudioCaptureError`) so the orchestrator can surface them as `pipeline.warning` / `pipeline.error` events instead of degrading silently.

**`src/audio_devices.py`** — Shared input-device resolution used by capture and pre-flight: never auto-selects a loopback/virtual device (BlackHole, Teams Audio, aggregates) as the microphone, `resolve_named_input_index()` fuzzy-matches a typoed configured device name (never onto a virtual device), and `refresh_input_devices()` re-initialises PortAudio so the long-running daemon sees current hardware (PortAudio otherwise freezes its device table at process start).

**`src/mic_permission.py`** — macOS microphone TCC introspection/request via ctypes AVFoundation (no pyobjc). The daemon is a bare launchd binary, so macOS never shows the permission prompt implicitly — opening input streams just yields zeros (RMS −100 dBFS) or `PortAudioError -9986`, and grants are path-bound (the MeetingMind→Context Recall rename silently orphaned the old grant). Every recording start is gated on `ensure_microphone_access()`; the boot path requests the prompt explicitly. `build_daemon.sh` signs the binary with a stable identifier so grants survive rebuilds. Tests must never fire the real prompt — `tests/conftest.py` forces `authorized`.

**`src/audio_cleanup.py`** — Temp-audio sweeper: removes 44-byte header-only stubs (any age) and `meeting_*.wav` older than `audio.temp_retention_days` from `temp_audio_dir`, sparing the in-flight capture. Runs at daemon boot and after each pipeline run.

**`src/audio_routing.py`** — Automatic system-audio routing. `CoreAudioBackend` (ctypes, no extra deps) + `AudioRouter`: at recording start, if the default output doesn't feed BlackHole, it finds-or-creates a managed Multi-Output Device ("Context Recall Audio" = current output + BlackHole), switches to it, and switches back after the meeting. Gated by `audio.auto_route_system_audio` (default on). Router tests use a fake backend; `tests/conftest.py` forces the real backend unavailable so the suite never mutates host audio state.

**`src/silent_input_detector.py`** — Per-source RMS watchdog. Emits a warning within seconds when BlackHole stops delivering audio (A1 fix).

**`src/transcriber.py`** — Wraps **`mlx_whisper`** (Apple Silicon only). Model loads lazily on first `transcribe()` call. Returns `Transcript` containing `TranscriptSegment` objects with timestamps and optional speaker labels.

**`src/live_transcriber.py`** — Same MLX engine but driven by chunked audio fed from the PortAudio callback. Worker thread runs `mlx_whisper.transcribe` every ~8s on the rolling buffer, deduplicates against the previous chunk's text, and emits per-segment events for the UI. `stop()` joins the worker with a 30s timeout, so callers (notably `_on_meeting_end`) dispatch it to a daemon thread rather than blocking the detector.

**`src/diariser.py`** — Energy-based speaker labelling. Compares RMS between system and mic source WAVs per segment to label "Me" vs "Remote". No ML dependencies. **`src/pyannote_diariser.py`** is an optional alternative backend; selected via `diarisation.backend` in config. The pyannote import is deferred until first use so the module is import-safe without `torch`/`pyannote.audio`.

**`src/summariser.py`** — Two backends: `"ollama"` (local, free, httpx POST to `/api/chat`) and `"claude"` (Anthropic API). Produces structured Markdown parsed into a `MeetingSummary`. Template-driven via `src/templates.py`.

**`src/output/`** — `MarkdownWriter` (Obsidian-compatible with YAML frontmatter) and `NotionWriter` (native Notion blocks via API).

### API server

**`src/api/server.py`** — `ApiServer` class. Spins up a uvicorn server in a background thread on the orchestrator's lifecycle. Exposes its asyncio loop as `self.loop` so the pipeline thread can use `asyncio.run_coroutine_threadsafe` to write to the DB without owning an event loop. Also owns `self.repo` (the `MeetingRepository`) and a connection-manager (`src/api/websocket.py`) used for real-time pipeline events.

**`src/api/routes/`** — 20 router modules (status, meetings, config, recording, devices, diagnostics, support_bundle, export, resummarise, reprocess, models, templates, search, speakers, calendar, action_items, series, analytics, notifications, prep). Each registers under bearer-token auth (`src/api/auth.py`). The orchestrator emits pipeline lifecycle events (`pipeline.stage`, `pipeline.warning`, `pipeline.error`, `pipeline.complete`, `transcript.segment`) via the WebSocket event bus; the UI drives all its state off those plus REST polls.

**`src/api/routes/reprocess.py`** — POST `/api/meetings/{id}/reprocess`. Submits the pipeline as a background task and returns 202 immediately (C4 fix) so long re-transcriptions don't time out the HTTP client. Mirrors the orchestrator's empty/short-transcript contract from `_process_audio` (B1 unification).

### DB

**`src/db/database.py` + `src/db/repository.py`** — SQLite via `aiosqlite`. Migrations are numbered; `tests/test_db_migration_v9.py` is the latest. Schema covers meetings, segments, attendees, speaker mappings, templates, action items, analytics rollups, prep briefings, series memberships, notification dispatches, and an FTS5 mirror for full-text search. `segment_embeddings_vec` is a `sqlite-vec` virtual table populated by `src/embeddings.py` for semantic search.

### Intelligence modules

These run after the core pipeline finishes (via `_run_post_processing`), each non-fatal:

- **`src/action_items/`** — extractor (LLM-driven) + repository.
- **`src/analytics/`** — `AnalyticsEngine` rolls up per-period counters (meeting count, action-item completion, etc.) and persists snapshots.
- **`src/prep/`** — pre-meeting briefing generator (uses calendar context + history).
- **`src/series/`** — meeting-series detection and grouping.
- **`src/calendar_matcher.py`** — matches the active recording to a calendar event (uses macOS Calendar via EventKit when available). When a match is found, attendees are stored as candidate speaker labels and the orchestrator may auto-rename "Remote" in 2-person meetings.
- **`src/notifications/`** — dispatches outbound notifications (channels under `src/notifications/channels/`).
- **`src/embeddings.py`** — `Embedder` wrapping a local sentence-transformer; called per-segment after diarisation to populate `segment_embeddings_vec`.

### UI

**`ui/src-tauri/`** — Tauri 2.x Rust shell (tray icon, native menus, updater plugin, notification plugin, opener plugin). Bundles a PyInstaller-built daemon as a sidecar resource. `tauri.conf.json` declares `resources/context-recall-daemon` — keep that directory present (even if empty) for `cargo check` to succeed.

**`ui/src/`** — React 19 + TypeScript + Vite 7 + TanStack Query + Zustand + Tailwind 4. Tests via Vitest 4. State stores live in `ui/src/stores/`, derived state in `ui/src/lib/`, hooks in `ui/src/hooks/`, screens in `ui/src/components/<area>/`. The daemon connection / pipeline-stage state machine is driven by `usePipelineSync` reading WebSocket events; `appStore` mirrors the meeting list and current pipeline status for offline-tolerant UI updates.

### Config

**`src/utils/config.py`** — Typed dataclass config loaded from `config.yaml`. `_build_dataclass()` ignores unknown keys for forward-compatibility. Paths with `~` are expanded via `_expand_path()`. Sections: `detection`, `audio`, `transcription`, `summarisation`, `diarisation`, `calendar`, `markdown`, `notion`, `logging`, `api`, `retention`, `action_items`, `series`, `analytics`, `notifications`.

## Key Constraints

- **macOS + Apple Silicon only**: relies on BlackHole virtual audio driver, `pgrep`, `lsof`, `osascript`, and `mlx_whisper` (MLX is Apple-Silicon only). The CI matrix marks the MLX/Tauri jobs as Apple-Silicon only (commit `554ede5`).
- **`config.yaml` is gitignored** — contains API keys. `config.example.yaml` is the tracked template.
- **Python tests**: pytest + pytest-asyncio. `python3 -m pytest tests/ -v`. ~690 tests.
- **UI tests**: vitest 4. `cd ui && npm test`. Pure UI; Tauri shell is not booted.
- **Rust check**: `cd ui/src-tauri && cargo check` (requires the daemon-resource stub above).
- **Linting**: ruff for Python (`ruff check src/ tests/`); tsc for TypeScript (`cd ui && npx tsc --noEmit`).
- **`httpx`** is used by the Ollama backend but is an implicit dependency (installed transitively via `anthropic`).
- Audio callbacks run on PortAudio threads — each writes to its own `sf.SoundFile` exclusively.
- `AudioCapture.stop()` blocks up to 30s while post-merge runs; pass `blocking=False` to defer the wait (the orchestrator does, then calls `wait_for_merge`).
- `LiveTranscriber.stop()` joins its worker (up to 30s). Never call it on the detector callback thread — dispatch to a daemon thread.
- MLX Whisper models download automatically on first use; size depends on `transcription.model_size`.
- DB writes from the pipeline thread go through `asyncio.run_coroutine_threadsafe(...)` on `self._api_server.loop`. If the loop is closed mid-pipeline, `_db_update` logs an ERROR with the meeting id and dropped fields (C3 fix) — never silently drops.
- Status-transition correctness on the API path is best tested via the `app_with_mocked_api` fixture in `tests/test_orchestrator.py` (X6); leaving `_api_server = None` short-circuits both `_persist_audio` and `_db_update` to no-ops.
