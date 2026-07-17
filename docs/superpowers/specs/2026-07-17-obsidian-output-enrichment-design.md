# Enrich Context Recall's Obsidian note output

**Date:** 2026-07-17
**Status:** Design approved, pending spec review
**Branch:** `feat/obsidian-output-enrichment` (off `main`)
**Source brief:** `context-recall-obsidian-output-spec.md` (Jamie's grounding brief)

## Objective

Make the single Markdown note that Context Recall writes per meeting a first-class
Obsidian artefact that reaches frontmatter and content parity with the enriched
"Circleback" notes already in Jamie's PARA vault, so both pipelines converge on one
vault convention. Today the Context Recall note is written from the summary only,
before post-processing runs, so client/project tags, structured action items,
resolved attendees, insights and talk stats never reach the note. It also lands flat
in `70 Meetings/Unsorted/`, inlines the full transcript, and uses freeform tags the
Dataview dashboards cannot pick up.

The gold reference is a real note in the vault:
`70 Meetings/Siemens/2026-07-07 - Siemens - Weekly Managed Professional Services Review.md`.

## Global constraints (apply to every phase)

- Do not touch or reprocess existing Circleback notes. This work changes the writer,
  not the vault contents. Any migration of already-written Context Recall notes is a
  separate opt-in script, not part of the daemon path.
- No em dashes anywhere in code, comments, or emitted note content. Use commas, colons,
  or restructure.
- United Kingdom English throughout writer-authored strings.
- Preserve existing code comments unless the code they describe is removed.
- Every new frontmatter list must serialise as a YAML block list, verified by a
  round-trip test. Inline flow lists or quoted strings corrupt the vault tooling.
- `enriched: true` is set only after post-processing has populated the note. Re-running
  the pipeline must be idempotent and must not overwrite a value a user has manually
  corrected.
- Keep the atomic write pattern already in `markdown_writer.py` (temp file plus
  `os.replace`).

## Confirmed facts (verified against the live vault, 2026-07-17)

- **`70 Meetings/` subfolders:** `Armacell`, `NTT`, `QVCCS Internal`, `Siemens`,
  `Unsorted`, `Old Meeting Archive`. There is no `Venetian` folder, although a
  `client/venetian` tag (1 use) and a `10 Projects/QVCCS - Work Projects/The Venetian`
  project note exist. Venetian meetings fall back to `Unsorted/` until a folder is added.
- **Tag taxonomy in use:** `client/{siemens, armacell, ntt, venetian}`,
  `project/{siemens-7, siemens-13, siemens-16, armacell, defect}`, and the flat
  `qvccs-internal`. Slugs are curated short forms (`siemens-16`, not
  `siemens-16-smart-uk-infrastructure`), so they cannot be derived by `slugify()` of
  the DB name.
- **Dashboard query** (`99 Dashboards/Meeting Action Items.md`):
  `TASK FROM "70 Meetings" WHERE !completed and (contains(text, "#client/") or
contains(text, "#project/"))`. A `## My Tasks` line only needs a `#client/*` or
  `#project/*` tag and to be incomplete; emoji and date placement are query-agnostic.
- **Owner identities to fold into `Jamie White (QVCCS)`:** the labels `Me` and `Jamie`,
  and the emails `jamiecs@live.co.uk`, `j65541761@gmail.com`.
- **Pipeline ordering:** `PipelineRunner._write_outputs()` (Step 6) runs before
  `_dispatch_post_processing()` (Step 7). `_append_notion_insights()` is the existing
  precedent for "fetch structured data after post-processing and rewrite the output".

## Decisions taken during brainstorming

1. **Taxonomy source of truth:** a curated config map in `MarkdownConfig`, not derived
   slugs and not a DB schema column. Unknown client routes to `Unsorted/` with no
   `client/*` tag and a surfaced warning.
2. **Transcript default mode:** `foldout` (collapsible `> [!quote]-` callout in the same
   note). Modes `linked`, `omit`, `inline` also supported.
3. **Delivery:** one feature branch, all six phases as independently shippable ordered
   commits (tests green per commit), then one draft PR.
4. **Action items section:** concise structured table (Action | Owner | Due | Status)
   AND the summary's per-item context kept as collapsible detail below the table.
5. **Gold-note divergences (following the brief, not the exact gold note):** keep `time`
   and `word_count` in frontmatter; `## Related` uses `- Previous:` / `- Project:`
   labels; render Decisions and Risks as `> [!info]` / `> [!warning]` callouts.

## Architecture

### The enabling change: two-pass write

The write moves from a single pre-enrichment pass to two passes keyed on the DB meeting
id (the `recall_id`, the idempotency key):

- **Pass 1 (Step 6, existing timing):** write the note immediately from the summary with
  `enriched: false`. The note appears in the vault the moment the meeting completes, so
  there is no regression in perceived latency.
- **Pass 2 (new, tail of `_post_process_async`):** a new `_rerender_markdown(meeting_id)`
  step, added after insights extraction and mirroring `_append_notion_insights`, fetches
  the meeting row plus the structured repos and rewrites the same file with
  `enriched: true`.

Idempotency and no-duplicate guarantees:

- The existing note is located by the stored `markdown_path` on the meeting row (set by
  `_write_outputs`). Re-render and reprocess both rewrite that file in place.
- If client routing (Phase 4) resolves a different folder on re-render, the file is moved
  atomically to the new folder and `markdown_path` is updated. Never a second file.
- Reprocess already recomputes tags, action items and insights; the tail re-render makes
  its note current too, via the same code path.

Manual-edit safety (mirrors the client/project "never overwrite manual" rule):

- The enriched writer owns the computed sections and the taxonomy frontmatter it
  derives from the DB. It does not overwrite a `client` or `project` frontmatter value
  when the meeting row's `assignment_source == 'manual'`; the manual assignment is the
  source of truth and flows through unchanged.
- `title` precedence continues to follow the existing `title_source` machinery: a
  manual rename is preserved (`preserve_title`), and the note filename follows it.

### New writer input: `NoteContext`

Instead of scraping `raw_markdown`, a structured `NoteContext` dataclass is passed into a
new `MarkdownWriter.write_note(context) -> Path | None`. The legacy `write(summary,
transcript, started_at, duration_seconds)` becomes a thin adapter that builds a minimal
pre-enrichment `NoteContext` (Pass 1), so existing callers and tests keep working.

`NoteContext` fields:

- Identity and timing: `recall_id`, `title`, `date`, `time`, `duration_minutes`,
  `word_count`, `started_at`.
- Taxonomy: `client_name`, `client_folder`, `client_tag`, `project_name`, `project_tag`,
  `extra_tags` (topic tags, for example `qvccs-internal`), `meeting_type`.
- People: `attendees` (list of resolved display names), `owner_display_name`.
- Structured content: `action_items` (all, structured), `owner_tasks` (owner's
  incomplete items), `insights` (results list), `talk_stats` (from `compute_talk_stats`).
- Narrative: `summary_sections` (the `{heading: body}` map parsed from the summary).
- Linking: `related_links` (resolved, existing-only wikilinks).
- Transcript: `transcript`, `transcript_mode`.
- Flags: `enriched`.

The pipeline builds `NoteContext` on the API loop (where DB access lives), then calls the
synchronous writer on a worker thread, the same threading model the pipeline already uses.

### Note assembly: narrative versus structured sections

A small `src/output/note_assembler.py` composes the body. It:

1. Splits the summary's `raw_markdown` into a `{heading: body}` map using the same simple
   `##`-heading idiom as `MeetingSummary.from_markdown`.
2. Canonicalises headings to the gold set. The built-in `standard` template is lightly
   updated so its output aligns 1:1: `Summary -> Executive summary`,
   `Discussion Points -> Discussion points`, `Key Decisions -> Decisions made`, and
   `Open Questions & Risks` is split into `Open questions` plus `Risks and blockers`.
   Unknown or custom headings pass through in place (tolerant of Ollama drift and custom
   templates); missing sections are simply skipped.
3. Drops sections the writer now owns: `Participants` (folds into the overview table),
   `Action Items` (replaced by the structured table plus collapsible context), `Tags`
   (moves to frontmatter).
4. Emits in gold order:
   `## Related` -> `## Meeting overview` (table) -> `## Executive summary` ->
   `## Discussion points` -> `## Decisions made` (callouts) -> `## Action items`
   (table + collapsible context) -> `## Open questions` -> `## Risks and blockers`
   (callouts) -> `## Insights` -> `## Talk time` (table) -> `## My Tasks` ->
   `## Notable quotes` -> footer -> transcript (per mode).

Rendering the `standard` template's new headings changes what is stored in
`summary_markdown` and what Notion renders; both are cosmetic there.
`MeetingSummary.from_markdown` still scrapes the title from the first H1 and tags from a
`## Tags` line, both unaffected.

## Phase plan

Each phase is an independently shippable commit with passing tests. Land in order.

### Phase 1: re-render after enrichment (the enabling change)

- Add `NoteContext` and `MarkdownWriter.write_note`; refactor `write()` to the adapter.
- Add `PipelineRunner._rerender_markdown(meeting_id)` at the tail of
  `_post_process_async`, gated on `config.markdown.enabled`, non-fatal, building a full
  `NoteContext` from the meeting row and repos.
- Locate the existing note by `markdown_path`; rewrite in place; set `enriched: true`.
- Reprocess safe: the same re-render runs at the tail of a reprocess run.

**Acceptance:** a processed meeting produces one note whose frontmatter and body carry
the post-processing data; a reprocess updates that same file rather than duplicating it.

### Phase 2: frontmatter parity and hierarchical tags

- Frontmatter becomes: `title, date, time, client, project, meeting_type,
duration_minutes, word_count, attendees (block list), tags (block list),
source: context-recall, recall_id, enriched`.
- A dedicated YAML dump path forces block-list style for `attendees` and `tags`; a
  round-trip test reads the emitted file back and asserts both parse as lists.
- Hierarchical tags come from the taxonomy map: `client/<slug>` and `project/<slug>` plus
  any flat topic tags (for example `qvccs-internal`). The writer never invents a
  `client/*` or `project/*` tag; an unknown client or project is left off and surfaced
  via a `pipeline.warning`.
- Attendee resolution: fold owner identities to `owner_display_name`
  (`Jamie White (QVCCS)`); resolve others via `src/people/`, `src/voice/`,
  `src/calendar_matcher.py`; leave a label unresolved rather than guess.

**Acceptance:** a Context Recall note and a Circleback note for the same client are
indistinguishable at the frontmatter level (modulo the intentional `source`/`recall_id`
versus `circleback_id`/`circleback_url` difference).

### Phase 3: `## My Tasks` wired into the dashboard

- Render `## My Tasks`: the owner's own incomplete action items as Tasks-plugin
  checkboxes.
- Line format: `- [ ] <action text> #client/<x> #project/<y> <priority emoji> 📅 <due>`.
  Priority map: `urgent -> 🔺`, `high -> ⏫`, `medium -> 🔼` (default), `low -> 🔽`.
  Include `📅 YYYY-MM-DD` only when a due date exists.
- Tags on the task line are the same `client/*` and `project/*` tags as the frontmatter,
  so the dashboard query matches with no manual editing.
- Owner selection uses the `owner_identities` config to match the action item `assignee`.
  Non-owner items stay in the `## Action items` table only.

**Acceptance:** a new Context Recall note with an open owner task appears in the Meeting
Action Items dashboard without any manual editing.

### Phase 4: client subfolder routing and transcript handling

- `route_by_client` (default on) files the note into `70 Meetings/<folder>/` from the
  taxonomy map, falling back to `Unsorted/` when the client is unknown. `vault_path`
  stays the `70 Meetings` base.
- On re-render, if the resolved folder differs from the current location, move the note
  atomically and update `markdown_path`.
- `transcript_mode` config: `foldout` (default, collapsible `> [!quote]-` callout),
  `linked` (companion `<title> (transcript).md` with a wikilink), `omit`, `inline`
  (current behaviour). `include_full_transcript: true` maps to `inline` for backward
  compatibility during config load.

**Acceptance:** notes file themselves into the correct client folder; the default note is
no longer dominated by a giant transcript.

### Phase 5: cross-linking and visual polish

- `## Related` directly under the H1 with labelled wikilinks:
  `- Previous: [[...]]` (previous instance of the same recurring meeting, resolved via
  `src/series/` plus same client folder and an earlier date) and `- Project: [[...]]`
  (the `10 Projects` note when one exists). Only link notes that actually exist; resolve
  against the vault filesystem, never create placeholder links. Omit `## Related`
  entirely when nothing resolves.
- Convert attendee and owner names and the project to `[[wikilinks]]` when a matching
  person or project note exists.
- Render Decisions and Risks as callouts (`> [!info]`, `> [!warning]`).
- Write the insights section via the existing `render_insights_section()`.
- Add a `## Talk time` table from `src/talk_stats.compute_talk_stats`.

**Acceptance:** a note links back into the vault graph and reads cleanly in preview mode.

### Phase 6: config surface and hygiene

- Extend `MarkdownConfig`: `route_by_client` (bool, default true),
  `client_taxonomy` (map: client name -> `{folder, tag}` plus a project name -> tag map),
  `transcript_mode` (str, default `foldout`), `emit_my_tasks` (bool, default true),
  `owner_identities` (list of str), `owner_display_name` (str), and a `rerender`
  toggle if useful. Every new field gets a sensible default and an inline comment for
  non-obvious values. `_build_dataclass` already ignores unknown keys for
  forward-compatibility.
- Remove the em dash from the footer; use a comma.
- United Kingdom English in writer-authored strings.

## Target note (reference skeleton)

```markdown
---
title: Morning Standup Call
date: 2026-07-15
time: "10:03"
client: QVCCS Internal
project: ""
meeting_type: Standup
duration_minutes: 28
word_count: 4456
attendees:
  - Jamie White (QVCCS)
  - Amelia Lawton (QVCCS)
  - Seb (QVCCS)
tags:
  - qvccs-internal
  - project/siemens-16
source: context-recall
recall_id: 4f2a...
enriched: true
---

# Morning Standup Call

## Related

- Previous: [[2026-07-14 - QVCCS Internal - Morning Standup Call]]
- Project: [[Project Siemens 16 Smart UK Infrastructure]]

## Meeting overview

| Field     | Detail                                                  |
| --------- | ------------------------------------------------------- |
| Date      | Tuesday, 15 July 2026                                   |
| Duration  | ~28 minutes                                             |
| Attendees | Jamie White (QVCCS), Amelia Lawton (QVCCS), Seb (QVCCS) |
| Purpose   | ...                                                     |

## Executive summary

...

## Discussion points

...

## Decisions made

> [!info] Decision
> ...

## Action items

| Action | Owner | Due | Status |
| ------ | ----- | --- | ------ |
| ...    | ...   | ... | Open   |

> [!note]- Action item detail
> ...

## Open questions

...

## Risks and blockers

> [!warning] ...

## Insights

...

## Talk time

| Speaker             | Talk time | Turns |
| ------------------- | --------- | ----- |
| Jamie White (QVCCS) | 12m 04s   | 34    |

## My Tasks

- [ ] Rebuild the callback logic in the in-queue call flow #project/siemens-16 🔼 📅 2026-07-18

## Notable quotes

...

> [!quote]- Full transcript
> **00:00** _Jamie White_: ...

---

_Generated by Context Recall on 2026-07-15 10:31, 28 min, 4,456 words_
```

## Files to touch

- `src/output/markdown_writer.py`: `NoteContext`, `write_note`, `write` adapter,
  frontmatter builder, block-list YAML dump, atomic move on re-route.
- `src/output/note_assembler.py` (new): section split, heading canonicalisation, body
  assembly, callouts, tables, My Tasks, transcript modes, Related.
- `src/pipeline_runner.py`: `_rerender_markdown` at the tail of `_post_process_async`;
  helper to build `NoteContext` from the meeting row and repos; reprocess parity.
- `src/templates.py`: rename `standard` template headings to the gold set; split
  Open Questions and Risks.
- `src/utils/config.py`: extend `MarkdownConfig`.
- `src/output/taxonomy.py` (new, small): client/project name -> folder and tag slug
  resolution against `MarkdownConfig.client_taxonomy`, with unknown surfaced.
- `config.example.yaml`: document the new `markdown` fields and a seed taxonomy map.
- Tests under `tests/`: new test modules per phase plus a golden-file end-to-end test.

## Testing and acceptance

- `python3 -m pytest tests/ -v` (about 1180 tests) and `ruff check src/ tests/` green
  per phase.
- New tests: block-list frontmatter round-trip, My Tasks line format (dashboard-query
  match), client routing (known and unknown-surfaced), idempotent re-render (one file,
  reprocess updates in place), each transcript mode, attendee folding, taxonomy map.
- A golden-file test renders a fixture meeting end to end and asserts the note matches the
  target skeleton.
- Manual check on one real fixture: frontmatter parity with a Circleback note, a My Tasks
  line the dashboard query matches, correct client folder, no em dashes.

## Out of scope

- Migrating already-written Context Recall notes in the vault (separate opt-in script).
- Any change to Circleback notes.
- Notion writer content parity (only the archive-and-replace idempotency pattern is a
  reference, not a target).
