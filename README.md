# MeetingMind

A macOS daemon that automatically detects Microsoft Teams meetings, transcribes them locally using faster-whisper, and produces structured summaries with actionable task lists. Outputs to both a local Markdown vault (Obsidian-compatible) and Notion.

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                     MeetingMind Daemon                   │
│                                                          │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────────┐ │
│  │ Detector  │──▶│ Audio Capture │──▶│  Transcriber     │ │
│  │ (polling) │   │ (BlackHole)  │   │  (faster-whisper) │ │
│  └──────────┘   └──────────────┘   └────────┬─────────┘ │
│                                              │           │
│                                    ┌─────────▼─────────┐ │
│                                    │   Summariser      │ │
│                                    │   (Claude API)    │ │
│                                    └─────────┬─────────┘ │
│                                              │           │
│                              ┌───────────────┼────────┐  │
│                              ▼               ▼        │  │
│                         Markdown          Notion      │  │
│                          Vault             Page       │  │
│                              └───────────────┘        │  │
└─────────────────────────────────────────────────────────┘
```

## Why This Is Undetectable

Teams only notifies other participants when:
- A **recording is started via the Teams UI**
- A **bot joins** the meeting as a participant

MeetingMind does neither. It captures your local system audio via a loopback driver (BlackHole), which is functionally identical to you listening through your speakers. No network traffic is generated, no bot joins, and no Teams API is invoked. From the remote participants' perspective, nothing has changed.

## Prerequisites

### 1. BlackHole (Virtual Audio Driver)

BlackHole creates a virtual audio device that lets you capture system audio output.

```bash
brew install blackhole-2ch
```

After installation, create a **Multi-Output Device** in Audio MIDI Setup:
1. Open **Audio MIDI Setup** (Spotlight → "Audio MIDI Setup")
2. Click **+** → **Create Multi-Output Device**
3. Tick both your real speakers/headphones AND **BlackHole 2ch**
4. Set your real device as the **primary** (clock source)
5. Set this Multi-Output Device as your system output when using MeetingMind

This routes audio to both your ears and the virtual loopback simultaneously.

### 2. Python Environment

Requires Python 3.11+.

```bash
cd meeting-mind
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configuration

Copy and edit the config:

```bash
cp config.example.yaml config.yaml
```

Fill in:
- `anthropic_api_key` — for meeting summarisation
- `notion_api_key` + `notion_database_id` — for Notion output (optional)
- `vault_path` — path to your Obsidian vault (e.g. `~/Documents/SecondBrain/Meetings`)
- `blackhole_device_name` — usually `"BlackHole 2ch"`

### 4. faster-whisper Model

On first run, the configured Whisper model is downloaded automatically. For Apple Silicon Macs, `base.en` or `small.en` are recommended (good accuracy, low latency). If you have 16GB+ RAM, `medium.en` is noticeably better.

## Usage

### Run as a foreground process

```bash
python -m src.main
```

### Run as a background daemon (launchd)

```bash
# Install the launch agent
cp com.meetingmind.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.meetingmind.agent.plist
```

### Manual trigger (skip detection, record now)

```bash
python -m src.main --record-now
```

## Output

### Markdown (Obsidian Vault)

Each meeting produces a file like:

```
~/SecondBrain/Meetings/2026-04-07_standup-with-patrik.md
```

Containing:
- Metadata (date, time, duration, participants if detected)
- Full transcript
- AI-generated summary
- Extracted action items with assignees
- Key decisions

### Notion

A new page is created in your configured Notion database with the same structure, tagged and dated for easy querying.

## Project Structure

```
meeting-mind/
├── README.md
├── requirements.txt
├── config.example.yaml
├── com.meetingmind.agent.plist    # launchd agent for auto-start
├── src/
│   ├── __init__.py
│   ├── main.py                   # Entry point and orchestrator
│   ├── detector.py               # Teams meeting detection (macOS)
│   ├── audio_capture.py          # BlackHole loopback recording
│   ├── transcriber.py            # faster-whisper STT
│   ├── summariser.py             # Claude API summarisation
│   ├── output/
│   │   ├── __init__.py
│   │   ├── markdown_writer.py    # Obsidian-compatible .md output
│   │   └── notion_writer.py      # Notion API output
│   └── utils/
│       ├── __init__.py
│       └── config.py             # YAML config loader
```

## Legal Note

Recording meetings may have legal implications depending on your jurisdiction. Many places operate under "one-party consent" laws (the UK included), meaning you can record a conversation you are a participant in. However, you should verify the laws applicable to your situation and your organisation's policies.
