# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

### Python daemon + API server

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
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

### Stable microphone grant across rebuilds (self-signed daemon signing)

The daemon's macOS mic (TCC) grant is pinned to its code **Designated Requirement**. Ad-hoc signing produces a `cdhash` DR that changes every rebuild, so the grant dies on each deploy and recording silently captures zeros (RMS −100 dBFS on **both** mic and the BlackHole loopback — a denied mic makes CoreAudio zero all input streams). Run **once per machine**:

```bash
./scripts/setup_signing_cert.sh          # idempotent; --rotate to deliberately replace
```

This creates a per-machine self-signed identity (`CN="Context Recall Self-Signed"`) in your login keychain — the private key never leaves the keychain and nothing is committed. `build_daemon.sh` then auto-detects it (via `security find-certificate`, **not** `find-identity` — an untrusted cert is invisible to the latter) and signs the daemon with a **stable cert-leaf DR** (`identifier "dev.jamiewhite.contextrecall.daemon" and certificate leaf = H"…"`), so the grant survives all future rebuilds. Absent the cert (CI, fresh clones), the build **falls back to ad-hoc** — it never hard-fails. `CONTEXT_RECALL_SIGN_IDENTITY=<name>` still overrides.

Deploy sequence that preserves the stable signature: `build_daemon.sh` (stable-signs the daemon) → `npm run tauri build` (mangles the bundled copy) → `scripts/inject_daemon.sh "<app>"` (restores the pristine stable-signed daemon, re-seals **only** the outer app). Nothing after `build_daemon.sh` re-signs the daemon. After installing, `launchctl bootout` then `bootstrap` (not `kickstart`), then click **Allow** once — the grant persists thereafter. Because switching from the old ad-hoc cdhash DR to the cert-leaf DR is a new identity, seed the grant once: if a stale entry lingers, `tccutil reset Microphone dev.jamiewhite.contextrecall.daemon` then re-Allow (a reboot reliably re-seats the prompt).

**Notarized (Developer ID) signing — durable across OS updates.** Self-signed grants survive rebuilds but **not** macOS OS updates (observed 2026-07-21: macOS 26.6 wiped the daemon's Calendar/Mic grants; every in-session re-grant path failed). The durable fix is a Developer-ID-signed, Hardened-Runtime, **notarized + stapled** build. `scripts/signing_lib.sh` resolves the identity in tiers: explicit `CONTEXT_RECALL_SIGN_IDENTITY` → a "Developer ID Application" cert (→ Hardened Runtime + secure timestamp + `scripts/daemon.entitlements`, notarizable) → the self-signed cert → ad-hoc. **This entire path requires a paid Apple Developer Program membership (~$99/yr).** A free "Personal Team" Apple ID (Xcode shows "Personal Team", and Manage Certificates offers only "Apple Development") **cannot** create a Developer ID certificate or notarize — enroll at developer.apple.com/account first. Absent enrollment the pipeline degrades cleanly to the self-signed tier (survives rebuilds, not OS updates). Local one-time setup: create a **Developer ID Application** cert (Xcode → Settings → Accounts → Manage Certificates → **+**; **not** "Apple Development") + an **App Store Connect API key** (Developer role), then run `scripts/setup_notary_profile.sh`. Notarized deploy sequence: `build_daemon.sh` → `npm run tauri build` (with `APPLE_SIGNING_IDENTITY` set to the Developer ID identity, so the app **and its nested frameworks** are hardened-signed) → `scripts/inject_daemon.sh "<app>"` (re-signs **only** the outer app Developer ID + Hardened Runtime; the nested daemon keeps its own signature — inside-out order) → rebuild the DMG (`hdiutil create …`) → `scripts/notarize_and_staple.sh "<dmg>"`. Notarization must run **once, after all content is final** (re-signing invalidates the ticket). Verify with `scripts/verify_signing.sh "<app>"` (expect `spctl` "accepted, source=Notarized Developer ID"). The OS-update-survival benefit is only observable at the next OS update — the design implements Apple's documented durable path, it can't force the OS's hand.

Do **not** sign with the keychain's "Apple Development" identity — without an embedded provisioning profile tccd rejects the bundle and kills the daemon (`OS_REASON_TCC`). GitHub-released daemons are Developer-ID-signed + notarized when the CI `APPLE_*` secrets are configured (`APPLE_CERTIFICATE` + `APPLE_CERTIFICATE_PASSWORD` for signing; `APPLE_API_KEY_P8` + `APPLE_API_KEY_ID` + `APPLE_API_KEY_ISSUER_ID` for notarization — set **all** of them together, and `TAURI_SIGNING_PRIVATE_KEY` too if the updater is used; `APPLE_TEAM_ID` is **not** needed — API-key notarization and CN-based codesign don't use it); absent the secrets they fall back to ad-hoc/unsigned and CI never hard-fails.

**If the mic dialog never appears at all**: check `codesign --verify` on the _installed_ daemon bundle. Runtime `__pycache__` writes (torch/speechbrain ship as source and compile on import) add files inside the signed bundle, breaking the resource seal — and tccd **silently refuses to prompt** for a bundle that fails validation (no dialog, permission pinned at `not_determined`, `tccutil reset` doesn't help). Fix: `find "<daemon .app>" -type d -name __pycache__ -exec rm -rf {} +`, verify the seal passes, restart the daemon. The frozen entrypoint disables bytecode writing (`src/utils/frozen_runtime.py`) so bundles built after 2026-07-14 can't re-break themselves.

### Tests and lint

```bash
# Python
pip install -r requirements-dev.lock
python3 -m pytest tests/ -v            # Full Python suite (~1180 tests)
python3 -m pytest tests/ -x            # Stop on first failure
ruff check src/ tests/                 # Lint check

# UI
cd ui
npm test                               # vitest run (~155 tests)
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

**`src/main.py`** — Orchestrator (`ContextRecall` class). Wires components together, manages lifecycle, owns the embedded `ApiServer` reference. Detector callbacks fire `_on_meeting_start` / `_on_meeting_end`; the latter dispatches the live-transcriber join to a daemon thread (so the detector callback returns promptly — see X4 fix in `654ecff`) and submits `_process_audio` to a `ThreadPoolExecutor`. `_process_audio` handles capture-specific pre-steps (merge wait, capture errors) then delegates to the shared `PipelineRunner`.

**`src/pipeline_runner.py`** — The shared post-capture pipeline (transcribe → diarise → voice-ID → speaker enrichment → client/project pre-assignment → summarise → persist/FTS → embeddings → writers → post-processing). Both the orchestrator and the reprocess route drive this class, so the two paths cannot drift. `DbBridge` marshals repo coroutines onto the API loop from pipeline threads. Post-processing (action items, speaker-name suggestions, LLM auto-tagging, tracker scans, analytics) runs async on the API loop with blocking LLM calls pushed to threads.

**`src/detector.py`** — State machine (IDLE → ACTIVE → ENDING) with debounce. Delegates platform-specific detection (`pgrep`, `lsof`, `osascript`) to `src/platform/` implementations via the `PlatformDetector` protocol.

**`src/platform/`** — Platform abstraction layer. `PlatformDetector` protocol with `MacOSDetector` (the only implementation that actually works), plus Linux/Windows stubs.

**`src/audio_capture.py`** — Records BlackHole (system audio) and microphone to **separate WAV files** on independent `sd.InputStream` threads, then post-merges with RMS normalisation. This avoids clock-drift between the two devices. Source files can be kept for diarisation. `start()` exposes `last_warning` (e.g. mic fallback) and `last_error` (typed `AudioCaptureError`) so the orchestrator can surface them as `pipeline.warning` / `pipeline.error` events instead of degrading silently.

**`src/audio_devices.py`** — Shared input-device resolution used by capture and pre-flight: never auto-selects a loopback/virtual device (BlackHole, Teams Audio, aggregates) as the microphone, `resolve_named_input_index()` fuzzy-matches a typoed configured device name (never onto a virtual device), and `refresh_input_devices()` re-initialises PortAudio so the long-running daemon sees current hardware (PortAudio otherwise freezes its device table at process start).

**`src/mic_permission.py`** — macOS microphone TCC introspection/request via ctypes AVFoundation (no pyobjc). The daemon is a bare launchd binary, so macOS never shows the permission prompt implicitly — opening input streams just yields zeros (RMS −100 dBFS) or `PortAudioError -9986`, and grants are path-bound (the MeetingMind→Context Recall rename silently orphaned the old grant). Every recording start is gated on `ensure_microphone_access()`; the boot path requests the prompt explicitly. The TCC grant is pinned to the daemon's code **Designated Requirement**: ad-hoc signing gives a `cdhash` DR that changes every rebuild (grant dies on each deploy), so `scripts/setup_signing_cert.sh` establishes a per-machine self-signed identity whose **cert-leaf DR is stable** — see "Stable microphone grant" below. Tests must never fire the real prompt — `tests/conftest.py` forces `authorized`.

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

**`src/api/routes/`** — 29 router modules (status, meetings, config, recording, devices, diagnostics, support_bundle, export, resummarise, reprocess, models, templates, search, speakers, people, clients, ask, meeting_insights, trackers, calendar, action_items, series, analytics, notifications, prep, auth, automations, insights, preflight). `insights` and `automations` are the custom-insight and rules-engine routes (see Intelligence modules). Each registers under bearer-token auth (`src/api/auth.py`). The orchestrator emits pipeline lifecycle events (`pipeline.stage`, `pipeline.warning`, `pipeline.error`, `pipeline.complete`, `transcript.segment`) via the WebSocket event bus; the UI drives all its state off those plus REST polls.

**`src/api/routes/reprocess.py`** — POST `/api/meetings/{id}/reprocess`. Submits the FULL shared pipeline as a background task and returns 202 immediately (C4 fix). Recovers surviving source WAVs from the temp dir for diarisation, re-applies stored speaker renames, archives + replaces the previous Notion page (`meetings.notion_page_id`), replaces extracted action items, and refreshes the meeting's own analytics period.

### DB

**`src/db/database.py` + `src/db/repository.py`** — SQLite via `aiosqlite`. Migrations are numbered (`SCHEMA_VERSION` is the head — currently **23**; `tests/test_db_migration_v23.py` is the latest). Schema covers meetings (incl. `notion_page_id` and client/project assignment columns), segments, speaker mappings (person-linked), people + voice profiles, clients + projects, keyword trackers + hits, templates, action items, analytics rollups, prep briefings, series memberships, notification dispatches, insight definitions + results (custom and structured LLM insights), automation rules + dispatches, an `app_metadata` key/value store, and an FTS5 mirror for full-text search. `segment_embeddings_vec` is a `sqlite-vec` virtual table populated by `src/embeddings.py` for semantic search.

### Intelligence modules

These run after the core pipeline finishes (via `_run_post_processing`), each non-fatal:

- **`src/action_items/`** — extractor (LLM-driven) + repository.
- **`src/analytics/`** — `AnalyticsEngine` rolls up per-period counters (meeting count, action-item completion, etc.) and persists snapshots.
- **`src/prep/`** — pre-meeting briefing generator (uses calendar context + history).
- **`src/series/`** — meeting-series detection and grouping.
- **`src/calendar_matcher.py`** — matches the active recording to a calendar event (uses macOS Calendar via EventKit when available). When a match is found, attendees are stored as candidate speaker labels and the orchestrator may auto-rename "Remote" in 2-person meetings.
- **`src/notifications/`** — dispatches outbound notifications (channels under `src/notifications/channels/`).
- **`src/embeddings.py`** — `Embedder` wrapping a local sentence-transformer; called per-segment after diarisation to populate `segment_embeddings_vec`.
- **`src/people/`** — persistent people directory (repository + LLM `suggester.py` that detects self-introductions and stores `candidate:` speaker suggestions).
- **`src/voice/`** — ECAPA voice recognition: `embedder.py` (SpeechBrain, lazy, guarded — degrades without speechbrain), `recognition.py` (pure numpy clustering + profile matching over unresolved labels like `Remote`/`SPEAKER_NN`), `enrolment.py` (builds profile samples from a labelled speaker's segments).
- **`src/tagging/`** — client/project store + auto-assignment: deterministic pre-pass (attendee email domains, calendar-title aliases, series inheritance) before summarisation with description injection into the prompt (`Summariser.summarise(extra_context=...)`), LLM classifier in post-processing for the rest. Manual assignments are never overwritten.
- **`src/trackers/`** — keyword trackers: `scanner.py` (word-boundary matching) + repository; scanned in post-processing, reprocess-safe (`replace_hits_for_meeting`).
- **`src/talk_stats.py`** — pure per-speaker talk-time/turns/monologue computation from `transcript_json`.
- **`src/insights/`** — user-defined LLM insight extractions run per meeting (`insight_definitions`). Two output modes: **list** (a flat list of `{content, speaker}`) or **structured** (user-defined typed fields — text/number/date/boolean/list — coerced into one record + a human-readable rendering). `extractor.py` (dual Claude/Ollama backend, structured prompt returns a JSON object) + `repository.py` (definitions + reprocess-safe results; `replace_results_for_definition` scopes a re-run to one definition so an automation `run_insight` can't clobber the global step).
- **`src/automations/`** — post-meeting rules engine. Pure `evaluator.py` (conditions `tag`/`client`/`project`/`title_contains`/`attendee_domain`, combined `all`/`any`) → `executor.py` actions `apply_tag`/`notify`/`webhook`/`run_insight`/`send_notes`. `run_insight` runs a specific insight on matching meetings (ungated, reprocess-safe); `send_notes` POSTs a **Circleback-schema** payload (id/name/createdAt/duration/tags/attendees/notes/actionItems/insights) HMAC-signed as `x-signature` (gated on `run_side_effects`). Definitions targeted by an enabled `run_insight` rule are **excluded from the global insight step** (`_extract_insights`) so they run only on matching meetings. Rules seeded once at boot via `src/insights/seed.py` (idempotent, `app_metadata['insights_seed_version']`).

### UI

**`ui/src-tauri/`** — Tauri 2.x Rust shell (tray icon, native menus, updater plugin, notification plugin, opener plugin). Bundles a PyInstaller-built daemon as a sidecar resource. `tauri.conf.json` declares `resources/context-recall-daemon` — keep that directory present (even if empty) for `cargo check` to succeed.

**`ui/src/`** — React 19 + TypeScript + Vite 7 + TanStack Query + Zustand + Tailwind 4. Tests via Vitest 4. State stores live in `ui/src/stores/`, derived state in `ui/src/lib/`, hooks in `ui/src/hooks/`, screens in `ui/src/components/<area>/`. The daemon connection / pipeline-stage state machine is driven by `usePipelineSync` reading WebSocket events; `appStore` mirrors the meeting list and current pipeline status for offline-tolerant UI updates.

### Config

**`src/utils/config.py`** — Typed dataclass config loaded from `config.yaml`. `_build_dataclass()` ignores unknown keys for forward-compatibility. Paths with `~` are expanded via `_expand_path()`. Sections: `detection`, `audio`, `transcription`, `summarisation`, `diarisation`, `calendar`, `markdown`, `notion`, `logging`, `api`, `retention`, `action_items`, `series`, `analytics`, `notifications`, `prep`, `voice_id`, `tagging`, `insights`, `automations`, `auto_arm`.

## Key Constraints

- **macOS + Apple Silicon only**: relies on BlackHole virtual audio driver, `pgrep`, `lsof`, `osascript`, and `mlx_whisper` (MLX is Apple-Silicon only). The CI matrix marks the MLX/Tauri jobs as Apple-Silicon only (commit `554ede5`).
- **`config.yaml` is gitignored** — contains API keys. `config.example.yaml` is the tracked template.
- **Python tests**: pytest + pytest-asyncio. `python3 -m pytest tests/ -v`. ~1180 tests. Tests never load real ML models (sentence-transformers/speechbrain are faked or unavailable).
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
