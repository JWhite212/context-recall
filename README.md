<p align="center">
  <h1 align="center">MeetingMind</h1>
  <p align="center">
    A macOS daemon that automatically detects Microsoft Teams meetings, transcribes them locally, and produces structured AI-powered summaries — completely offline and invisible to other participants.
  </p>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-macOS-blue" alt="macOS">
  <img src="https://img.shields.io/badge/python-3.11%2B-green" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/transcription-faster--whisper-orange" alt="faster-whisper">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT License">
</p>

---

## Overview

MeetingMind runs silently in the background, watching for active Teams calls. When a meeting starts, it captures both sides of the conversation — remote participants via system audio loopback and your voice via the microphone — then runs the recording through local speech-to-text and an AI summariser to produce structured notes with action items, decisions, and key topics.

**Output formats:**
- **Markdown** — Obsidian-compatible `.md` files with YAML frontmatter (Dataview-queryable)
- **Notion** — Native database pages with headings, bullets, and to-do items

**Summarisation backends:**
- **Ollama** — Free, fully local, no API key needed
- **Claude API** — Anthropic's Claude for higher-quality summaries (requires API credits)

## How It Works

```
┌──────────────────────────────────────────────────────────────┐
│                      MeetingMind Daemon                      │
│                                                              │
│  ┌──────────┐    ┌───────────────┐    ┌───────────────────┐  │
│  │ Detector │───▶│ Audio Capture │───▶│   Transcriber     │  │
│  │ (macOS   │    │ (BlackHole +  │    │   (faster-whisper) │  │
│  │  polling) │    │  Microphone)  │    │                   │  │
│  └──────────┘    └───────────────┘    └────────┬──────────┘  │
│                                                │             │
│                                      ┌─────────▼──────────┐  │
│                                      │    Summariser      │  │
│                                      │  (Ollama / Claude) │  │
│                                      └─────────┬──────────┘  │
│                                                │             │
│                                ┌───────────────┼──────────┐  │
│                                ▼               ▼          │  │
│                           Markdown          Notion        │  │
│                            Vault             Page         │  │
│                                └───────────────┘          │  │
└──────────────────────────────────────────────────────────────┘
```

## Why Other Participants Can't Tell

Teams notifies participants when:
- A **recording is started via the Teams UI**
- A **bot joins** the meeting

MeetingMind does neither. It captures your local system audio via a loopback driver (BlackHole), which is functionally identical to listening through your speakers. No network traffic, no bot, no Teams API calls — from everyone else's perspective, nothing has changed.

> **Note:** Recording meetings may have legal implications depending on your jurisdiction. Many regions operate under "one-party consent" laws, meaning you can record a conversation you participate in. Verify the laws and policies that apply to you before use.

## Features

- **Automatic detection** — Monitors macOS process state and audio device usage to detect live Teams calls without manual intervention
- **Dual-source audio** — Captures both system audio (remote participants) and microphone (your voice) simultaneously, mixed into a single recording
- **Local transcription** — Uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2 backend) for fast, private, on-device speech-to-text
- **AI summarisation** — Produces structured summaries with title, key decisions, action items (with owners/deadlines), open questions, and topic tags
- **Multiple backends** — Choose between free local Ollama models or the Claude API for summarisation
- **Obsidian integration** — Markdown output with YAML frontmatter designed for Obsidian Dataview queries
- **Notion integration** — Creates native Notion database pages with proper headings, bullets, and to-do blocks
- **Three run modes** — Daemon (auto-detect), manual recording, or process an existing audio file
- **Configurable** — Single YAML config file controls every aspect of the pipeline

## Prerequisites

### 1. BlackHole (Virtual Audio Driver)

BlackHole creates a virtual audio device that captures system audio output via loopback.

```bash
brew install blackhole-2ch
```

After installation, create a **Multi-Output Device** in Audio MIDI Setup:

1. Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
2. Click **+** → **Create Multi-Output Device**
3. Check both your real speakers/headphones **and** BlackHole 2ch
4. Set your real device as the clock source
5. Set this Multi-Output Device as your system output (System Settings → Sound → Output)

This routes audio to both your ears and the virtual loopback simultaneously.

### 2. Ollama (Local AI — Recommended)

For free, fully local summarisation with no API key:

```bash
# Install Ollama
brew install ollama

# Pull a model (llama3.1:8b is a good default)
ollama pull llama3.1:8b

# Start the Ollama server (runs on port 11434)
ollama serve
```

> Alternatively, you can use the Claude API by setting `backend: "claude"` in the config and providing an Anthropic API key.

### 3. Python Environment

Requires Python 3.11+.

```bash
git clone https://github.com/YOUR_USERNAME/meeting-mind.git
cd meeting-mind
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Configuration

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` to set:

| Setting | Description |
|---------|-------------|
| `summarisation.backend` | `"ollama"` (free, local) or `"claude"` (API) |
| `summarisation.ollama_model` | Ollama model name (e.g. `"llama3.1:8b"`) |
| `summarisation.anthropic_api_key` | Your Anthropic key (only if using Claude) |
| `markdown.vault_path` | Path to your Obsidian vault meetings folder |
| `audio.blackhole_device_name` | Usually `"BlackHole 2ch"` |
| `audio.mic_device_name` | Microphone name (empty = system default) |
| `audio.mic_volume` | Mic gain relative to system audio (`0.0`–`2.0`) |

See [`config.example.yaml`](config.example.yaml) for the full reference with all options documented.

## Usage

### Daemon Mode (Auto-Detect Meetings)

```bash
python3 -m src.main
```

Polls for active Teams calls and automatically starts/stops recording. Intended for always-on background use.

### Manual Recording

```bash
python3 -m src.main --record-now
```

Starts recording immediately without waiting for Teams detection. Press `Ctrl+C` to stop — the recording is then transcribed and summarised.

### Process Existing Audio

```bash
python3 -m src.main --process /path/to/audio.wav
```

Skip recording entirely and run an existing audio file through the transcription → summarisation → output pipeline.

### Run as a Launch Agent (Auto-Start on Login)

```bash
# Edit com.meetingmind.agent.plist to set your actual paths, then:
cp com.meetingmind.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.meetingmind.agent.plist
```

To stop:

```bash
launchctl unload ~/Library/LaunchAgents/com.meetingmind.agent.plist
```

## Output

### Markdown

Each meeting produces a file like:

```
~/Documents/Meetings/2026-04-08_quarterly-planning-review.md
```

```yaml
---
title: "Quarterly Planning Review"
date: 2026-04-08
time: 14:30
duration_minutes: 45
tags: ["roadmap", "hiring", "q3-planning"]
type: meeting-note
---
```

Followed by the AI-generated summary with sections for Key Decisions, Action Items, Open Questions, and the full timestamped transcript.

### Notion

A new page is created in your configured Notion database with:
- **Properties:** Title, Date, Tags (multi-select), Status
- **Content:** Native Notion blocks — headings, bullets, and to-do items (not raw Markdown)

## Configuration Reference

<details>
<summary><strong>Full config.example.yaml</strong></summary>

```yaml
# Meeting Detection
detection:
  poll_interval_seconds: 3         # How often to check for active calls
  min_meeting_duration_seconds: 30 # Ignore very short calls
  process_names:                   # Teams process names to monitor
    - "Microsoft Teams"
    - "MSTeams"
    - "Teams"

# Audio Capture
audio:
  blackhole_device_name: "BlackHole 2ch"
  mic_device_name: ""              # Empty = system default microphone
  mic_enabled: true                # Capture your voice alongside system audio
  mic_volume: 1.0                  # 0.0–2.0 gain for microphone input
  sample_rate: 16000               # 16kHz mono — optimal for Whisper
  channels: 1
  temp_audio_dir: "/tmp/meetingmind"

# Transcription
transcription:
  model_size: "small.en"           # tiny.en | base.en | small.en | medium.en | large-v3
  compute_type: "auto"             # int8 on Apple Silicon
  language: "en"                   # "auto" for language detection
  cpu_threads: 0                   # 0 = auto-detect

# Summarisation
summarisation:
  backend: "ollama"                # "ollama" or "claude"
  ollama_base_url: "http://localhost:11434"
  ollama_model: "llama3.1:8b"
  anthropic_api_key: "sk-ant-..."  # Only needed for backend: claude
  model: "claude-sonnet-4-20250514"
  max_tokens: 4096

# Output: Markdown
markdown:
  enabled: true
  vault_path: "~/Documents/Meetings"
  filename_template: "{date}_{slug}.md"
  include_full_transcript: true

# Output: Notion
notion:
  enabled: false
  api_key: "ntn_..."
  database_id: ""
  properties:
    title: "Name"
    date: "Date"
    tags: "Tags"
    status: "Status"

# Logging
logging:
  level: "INFO"
  log_file: "~/Library/Logs/meetingmind.log"
```

</details>

## Project Structure

```
meeting-mind/
├── README.md
├── requirements.txt
├── config.example.yaml
├── com.meetingmind.agent.plist      # launchd agent for auto-start
└── src/
    ├── __init__.py
    ├── main.py                      # Entry point and orchestrator
    ├── detector.py                  # Teams meeting detection (macOS)
    ├── audio_capture.py             # Dual-source audio recording
    ├── transcriber.py               # faster-whisper speech-to-text
    ├── summariser.py                # AI summarisation (Ollama / Claude)
    ├── output/
    │   ├── __init__.py
    │   ├── markdown_writer.py       # Obsidian-compatible .md output
    │   └── notion_writer.py         # Notion database page output
    └── utils/
        ├── __init__.py
        └── config.py                # YAML config loader
```

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Audio capture | [sounddevice](https://python-sounddevice.readthedocs.io/) + [BlackHole](https://existential.audio/blackhole/) |
| Transcription | [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (CTranslate2) |
| Summarisation | [Ollama](https://ollama.com/) or [Claude API](https://docs.anthropic.com/) |
| Notion output | [notion-client](https://github.com/ramnes/notion-sdk-py) |
| Config | PyYAML with typed dataclasses |
| Platform | macOS only (BlackHole, pgrep, lsof, osascript) |

## Troubleshooting

### No audio captured (silent recording)

1. Verify BlackHole is installed: `brew list blackhole-2ch`
2. Check your system output is set to the Multi-Output Device (not directly to speakers)
3. Run `python3 -m sounddevice` to confirm BlackHole appears as an input device
4. Play a system sound while recording to test the loopback

### VAD removes all audio

The faster-whisper VAD filter discards segments it classifies as silence. If your audio is very quiet, the entire recording may be filtered out. Check that the Multi-Output Device is configured correctly and that your system volume is not muted.

### Transcription produces no words

If the recording file has content but transcription returns 0 words, this usually means the audio level is too low for VAD detection. Verify audio is flowing with:

```bash
python3 -c "
import sounddevice as sd, numpy as np
data = sd.rec(int(3 * 48000), samplerate=48000, channels=2, device='BlackHole 2ch', dtype='float32')
sd.wait()
print(f'Peak amplitude: {np.max(np.abs(data)):.6f}')
"
```

A peak above `0.001` indicates signal is present.

### Ollama connection refused

Ensure the Ollama server is running (`ollama serve`) and listening on the configured port (default: `http://localhost:11434`).

## License

MIT
