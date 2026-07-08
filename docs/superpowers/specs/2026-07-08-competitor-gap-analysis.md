# Competitor Gap Analysis — AI Meeting Assistants

**Date:** 2026-07-08
**Branch:** `feat/competitor-features` (off `origin/main`)
**Purpose:** Analyse Circleback (the user's current tool) and the broader market, then identify high-value feature gaps for Context Recall to close — prioritised by user value and feasibility for a **local-first, no-bot, macOS** app.

## Method & confidence

- **Circleback** — analysed from **primary sources**: the user's live Circleback account via MCP (support/feature docs + the user's actual tag taxonomy) plus circleback.ai's own pages. **High confidence.**
- **Market (Granola, Otter, Fireflies, Fathom, tl;dv, Avoma, Sembly, Read.ai, Meetily, Muesli)** — web research (24 sources, 119 extracted claims). The adversarial-verification/synthesis stage was **cut short by a session limit**, so a handful of market claims are single-source; they're flagged where load-bearing. Corroborated with domain knowledge.

## 1. The landscape splits into two camps

**Bot-based cloud** (Otter, Fireflies, Fathom, tl;dv, Avoma, Sembly, Read.ai, Supernormal): a bot joins the Zoom/Meet/Teams call, transcription + AI run in the cloud, strong collaboration / CRM / call-coaching analytics. Data leaves the device; a visible bot sits in the call.

**Local-first / no-bot** (Granola, Meetily, Muesli, **Context Recall**): capture device audio directly, nothing joins the call.

**Circleback straddles both** — it offers a bot _and_ a no-bot desktop app _and_ mobile _and_ file import, then layers a strong automation/insights engine on top.

**Where Context Recall sits:** it is (with Meetily) the most private option and, uniquely, **fully on-device** with **cross-meeting voice-ID** — genuinely _ahead_ of the field on privacy/intelligence. The gaps are almost entirely in the **workflow + structured-intelligence layer** on top, plus **capture robustness**.

## 2. Per-product snapshot

| Product                      | Capture                                                                                                            | Transcription                   | Diarization / speaker ID                                                          | Standout / best-at                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------ | ------------------------------- | --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| **Circleback**               | Bot (Zoom/Meet/Teams/Webex) **+ no-bot desktop app** (mic+system audio, auto-detect/-start/-end) + mobile + import | Cloud, ~100 langs, 95%+ claimed | Auto speaker names; **voice saved & auto-recognised in future calendar meetings** | **Automations** (conditions→chained actions), **custom AI Insights**, deep integrations |
| **Granola**                  | **No-bot, local Mac** system-audio+mic (like us)                                                                   | **Cloud** (uploads audio)       | Minimal — "Me"/"Them" split only                                                  | Context-aware note templates that merge _your typed notes_ with AI                      |
| **Otter**                    | Bot + mobile                                                                                                       | Cloud, real-time                | Speaker ID (needs training)                                                       | Real-time transcript + chat; accuracy drops on cross-talk                               |
| **Fireflies**                | **Visible bot** (Zoom/Teams/Meet)                                                                                  | Cloud                           | Speaker ID                                                                        | Huge integration/CRM catalog, conversation analytics                                    |
| **Fathom / tl;dv**           | Bot                                                                                                                | Cloud                           | Speaker ID                                                                        | Free-tier generosity; clip/highlight sharing                                            |
| **Avoma / Sembly / Read.ai** | Bot                                                                                                                | Cloud                           | Speaker ID                                                                        | Revenue-intelligence / coaching / scorecards                                            |
| **Meetily** (OSS)            | No-bot, local                                                                                                      | **On-device** Whisper/Parakeet  | **Paid-only** (PRO)                                                               | Fully local + Ollama; but diarization gated behind paid tier                            |
| **Muesli** (OSS)             | **No-bot, CoreAudio process-tap** (no BlackHole) + ScreenCaptureKit fallback                                       | **On-device** (ANE/CoreML)      | —                                                                                 | Driver-free capture; **Silero-VAD speech-boundary chunking**                            |

## 3. Where Context Recall already leads

- **Fully on-device transcription** (MLX-Whisper) — Granola and the cloud tools upload audio; Context Recall doesn't.
- **Cross-meeting ECAPA voice-ID** — Granola has none; Meetily's diarization is paid. This is a real moat.
- **Semantic search + "ask" over history**, clients/projects, keyword trackers, talk-time — a richer local knowledge base than most no-bot peers.
- **No bot, no per-seat cloud fee, data never leaves the Mac.**

## 4. Gap analysis (prioritised)

Ranked by **user value (V)** and **feasibility (F)** for the Python-daemon + Tauri/React, local-first architecture. Each notes the methodology and local-first compatibility.

### Tier 1 — high value, feasible, native to local-first

**G1 · Automations / rules engine** — _Circleback's flagship._ Conditions (tag / client / project / participant / invitee-domain / title, with and/or logic) → chained actions. **V: very high, F: high.** Local-first actions: apply-tags, run a summary template, extract a custom insight, export to Obsidian/Notion, **POST a signed webhook**, draft/send email. (CRM/Slack ride on the webhook → Zapier/Make, keeping the core local.) Directly matches the user's **Client/Project/Type** tagging. Methodology: a `rules` table + a small condition-evaluator run in `_run_post_processing`; reuse the existing event/post-processing hooks.

**G2 · Custom AI "Insights"** — user-defined structured extraction schemas (e.g. `Risks[]`, `Decisions[]`, `Customer details {company, size}`), run per meeting or per type, each result tied to speaker+timestamp. **V: high, F: high.** Methodology: user-defined schema → JSON-mode prompt to the existing Ollama/Claude summariser → store as structured rows; render on the meeting page. Differentiates on _local_ structured intelligence.

**G3 · Per-meeting-type summary templates** — different note structure per **Type** (Discovery / Implementation / Review), auto-selected from the type tag / calendar title. **V: high, F: high.** `templates.py` already exists; add per-type templates + an auto-select rule. Smallest lift, direct match to the user's workflow.

**G4 · Action items → assignee + status + due + push** — link extracted action items to the **people directory** (email), add status (open/done) and due date, and optionally push (webhook / Linear-style). **V: high, F: medium.** Builds on existing action-item extraction + people repo.

**G5 · Private notes / scratchpad** — a per-meeting note the user writes before/during/after that (a) stays with the meeting and (b) is _fed into_ the summariser as `extra_context` (e.g. "this client's name is spelled…"). **V: medium-high, F: high.** New column + editor; summariser already accepts `extra_context`.

**G6 · Import audio/video via the UI** — drag-drop a file → run the full pipeline (the daemon already does this via `--process`; just needs a UI + route). **V: medium, F: high.**

### Tier 2 — valuable, medium effort

**G7 · CoreAudio process-tap capture (retire BlackHole)** — capture system audio via the macOS 14.4+ **process-tap / tap-based aggregate** API (as Muesli does), with a ScreenCaptureKit fallback — **no BlackHole install, no fragile Multi-Output routing**. **V: high (reliability), F: medium-high.** This _eliminates the root cause of the "system audio not recorded" bug class_ (the whole `audio_routing` fragility). ctypes CoreAudio, gated behind a config flag with BlackHole as fallback.

**G8 · Integration actions (Slack / Notion / webhook)** — first-class automation actions beyond export. **V: high, F: medium.** Webhook is trivial + local-first-pure; Slack/Notion via their APIs.

**G9 · VAD-driven live chunking** — replace fixed ~8s live chunks with Silero-VAD speech-boundary cuts (from Muesli) — fewer mid-sentence breaks, better live UX. **V: medium, F: medium.**

**G10 · Tag descriptions + colours** — extend the just-shipped tags with a description (improves LLM auto-tagging) + colour. **V: medium, F: high.** Small.

**G11 · Weekly digest email** • **G12 · Bulk multi-select actions** in the meeting list — both **V: medium, F: high/medium.**

### Tier 3 — skip or incompatible with local-first

- **Meeting bots** — architecturally incompatible _and_ intentionally avoided; _not having a bot is a selling point._
- **Cloud collaboration / workspaces / real-time sharing** — conflicts with single-user local-first (local export/share-file is the compatible substitute).
- **Native CRM (Salesforce/HubSpot)** — do it via the webhook → Zapier/Make rather than baking OAuth CRM sync into a local app.
- **Mobile app** — separate platform, large effort, out of scope for a macOS desktop tool.

## 5. Recommended build sequence

Two coherent, high-leverage threads:

1. **Intelligence + workflow layer (matches the user's Client/Project/Type workflow):** **G3 (type templates) → G2 (custom insights) → G1 (automations)** — these compound: templates structure the notes, insights extract the structured data, automations route it. This is the Circleback parity that actually matters day-to-day.
2. **Capture reliability:** **G7 (process-tap capture)** — the single highest-leverage fix for the audio pain (retires BlackHole and the bug-#5 class entirely).

Everything is compatible with the no-bot, local-first, private architecture; nothing here requires sending data off-device.
