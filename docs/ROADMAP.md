# Roadmap — competitor-inspired candidates not yet implemented

Compiled 2026-07-08 from a survey of Otter.ai, Fireflies, Fathom,
Granola, Circleback, tl;dv, and MacWhisper/Superwhisper. The first
tranche (ask-your-meetings chat, follow-up email drafts, talk-time
analytics, keyword trackers, people/voice directory, client/project
auto-tagging) shipped; these are the assessed-but-deferred ideas.

## Strong candidates

- **Rough-notes enhancement (Granola's signature).** A per-meeting
  scratchpad the user types during the call; at pipeline time the LLM
  merges the notes with the transcript into the summary ("enhance my
  notes" rather than "generate notes"). Fits the existing resummarise
  route; needs a notes column + live editor in the UI.
- **Webhook / automation on meeting complete (Circleback).** A
  configurable POST with the meeting JSON (summary, action items,
  assignment) on `pipeline.complete`. The notifications webhook channel
  already exists — this is a payload format + per-event routing away.
- **Smart chapters / topic segmentation (tl;dv).** LLM pass that splits
  the transcript into titled chapters with timestamps; feeds the audio
  player as jump points. Cheap prompt, good UX win on long meetings.

## Worth considering later

- **Meeting cost / time analytics across clients.** Talk-time and
  duration rolled up per client/project — "6h with Acme this month".
  The assignment columns make this a query + a chart.
- **Snippet sharing.** Export a single segment/quote as text/markdown
  with attribution. (Audio/video clips à la Fathom are out of scope —
  audio-only, local-first.)
- **CRM sync.** Circleback/Fireflies' headline feature; for a personal
  tool this is the Notion writer plus the future webhook. Revisit if a
  real CRM enters the picture.

## Explicitly rejected

- **Cloud bots that join calls** — against the local-capture design.
- **Multi-user workspace features** — single-user tool.
- **Real-time in-meeting AI chat (Otter)** — live transcription exists;
  interactive mid-call AI adds GPU contention with capture+MLX for
  marginal value at this stage.
