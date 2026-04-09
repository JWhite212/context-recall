# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Application

```bash
# Setup
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml  # then edit with real values

# Three run modes
python3 -m src.main                    # Daemon: auto-detect Teams meetings
python3 -m src.main --record-now       # Manual: record immediately, Ctrl+C to stop
python3 -m src.main --process file.wav # Process an existing audio file

# Verify imports
python3 -c "from src.main import MeetingMind"

# Run tests
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v           # Full test suite
python3 -m pytest tests/ -x           # Stop on first failure
ruff check src/ tests/                # Lint check
```

## Architecture

MeetingMind is a macOS-only daemon that auto-detects Teams meetings, records audio, transcribes locally, and produces AI summaries. The pipeline is strictly sequential:

```
TeamsDetector → AudioCapture → Transcriber → Diariser → Summariser → Writers
```

**`src/main.py`** — Orchestrator (`MeetingMind` class). Wires all components together, manages lifecycle. The detector's callbacks trigger start/stop on the capture, then `_process_audio()` runs the rest of the pipeline sequentially.

**`src/detector.py`** — State machine (IDLE → ACTIVE → ENDING) with debounce. Delegates platform-specific detection (pgrep, lsof, osascript) to `src/platform/` implementations via `PlatformDetector` protocol.

**`src/platform/`** — Platform abstraction layer. `PlatformDetector` protocol with `MacOSDetector` (macOS), plus Linux/Windows stubs.

**`src/audio_capture.py`** — Records BlackHole (system audio) and microphone to **separate WAV files** on independent `sd.InputStream` threads, then post-merges with RMS normalisation. This architecture avoids clock-drift between the two audio devices. Source files can be kept for diarisation.

**`src/transcriber.py`** — Wraps faster-whisper (CTranslate2). Model loads lazily on first `transcribe()` call. Returns `Transcript` containing `TranscriptSegment` objects with timestamps and optional speaker labels.

**`src/diariser.py`** — Energy-based speaker labelling. Compares RMS between system and mic source WAVs per segment to label "Me" vs "Remote". No ML dependencies.

**`src/summariser.py`** — Two backends: `"ollama"` (local, free, httpx POST to `/api/chat`) and `"claude"` (Anthropic API). Produces structured Markdown parsed into `MeetingSummary`.

**`src/output/`** — `MarkdownWriter` (Obsidian-compatible with YAML frontmatter) and `NotionWriter` (native Notion blocks via API).

**`src/utils/config.py`** — Typed dataclass config loaded from `config.yaml`. `_build_dataclass()` ignores unknown keys for forward-compatibility. Paths with `~` are expanded via `_expand_path()`.

## Key Constraints

- **macOS only**: relies on BlackHole virtual audio driver, `pgrep`, `lsof`, and `osascript`.
- **`config.yaml` is gitignored** — contains API keys. `config.example.yaml` is the tracked template.
- **Test suite** uses pytest + pytest-asyncio. Run with `python3 -m pytest tests/ -v`.
- **Linting** uses ruff. Run with `ruff check src/ tests/`.
- **`httpx`** is used by the Ollama backend but is an implicit dependency (installed transitively via `anthropic`).
- Audio callbacks run on PortAudio threads — each writes to its own `sf.SoundFile` exclusively.
- The `stop()` method on `AudioCapture` blocks (up to 30s) while post-merge runs.
- faster-whisper models download automatically on first use (~500MB for `small.en`).
