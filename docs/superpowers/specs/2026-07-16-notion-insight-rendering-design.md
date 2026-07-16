# Notion Insight Rendering — Design

**Date:** 2026-07-16
**Status:** Approved
**Branch:** `feat/notion-insights`

## Background

`#78` shipped custom + structured insights and rendered them into the **markdown** export, but **deferred Notion**: `NotionWriter.write()` creates the meeting's Notion page during the _synchronous_ pipeline write, whereas insights are extracted later in _async_ post-processing (`_extract_insights` + `run_insight` automation actions). So at page-creation time the insights don't exist yet.

## Goal

Render the meeting's insights (list + structured) into its Notion page, matching the markdown export's format, without reintroducing the ordering problem.

## Approach

Append an "Insights" block group to the page **after** insights are finalized, as the **last step of `_post_process_async`** (i.e. after `_run_automations`, so `run_insight` results are included).

**Why appending once is idempotent per run (no delete-first needed):** the Notion page is _freshly created or archived-and-replaced_ in this same pipeline run's synchronous `_write_outputs` (without insights). So each run the page starts insight-free, and post-processing appends the current insights exactly once. Reprocess archives the old page and creates a new one, then appends fresh — no duplication.

## Components

### 1. `NotionWriter.append_insights(page_id: str, results: list[dict]) -> bool`

- Builds blocks: an H2 `Insights` heading, then per `definition_name` an H3 sub-heading followed by one bullet per result's `content` (human-readable for both list and structured modes — matches `render_insights_section` in the markdown writer).
- Appends via `client.blocks.children.append(block_id=page_id, children=batch)`, chunked to Notion's 100-blocks-per-request limit, each call wrapped in the existing `_call_with_retry`.
- Reuses `_heading_block`, `_bullet_block`, `_rich_text`, `_get_client`.
- Returns `True` on success; on `APIResponseError`/`HTTPResponseError` sets `last_error`, logs, returns `False` (mirrors `write()`). Empty `results` → no-op, returns `False`.

### 2. `PipelineRunner._append_notion_insights(meeting_id: str) -> None`

- New final step in `_post_process_async`, after `_run_automations`.
- Gated: only runs when `config.notion.enabled`, the meeting has a stored `notion_page_id`, and `insight_repo.results_for_meeting(meeting_id)` is non-empty.
- Guarded/non-fatal (its own try/except that logs a warning), consistent with the other post-processing steps.
- Runs the blocking Notion HTTP call off the event loop via `asyncio.to_thread`.
- Reads the `notion_page_id` from the stored meeting record (set by the sync write / reprocess archive-replace earlier this run).

## Rendering (matches markdown export)

```
## Insights            (H2)
### <definition name>  (H3)
- <content>            (bullet)
- <content>
### <another definition>
- <content>
```

## Testing (TDD)

- **`NotionWriter.append_insights`** (fake Notion client): builds the expected block sequence (H2 + H3 per definition + content bullets), calls `blocks.children.append` with the right `block_id`; empty results → no append, returns False; grouping by `definition_name`; >100 blocks batched.
- **`PipelineRunner._append_notion_insights`** gating: no-op when notion disabled / no `notion_page_id` / no results; appends when all present; a Notion failure is swallowed (non-fatal).

## Out of scope (YAGNI)

- No delete-first / dedup of prior insight blocks (page is fresh each run).
- No richer per-field structured layout (toggles/tables) — content bullets match the markdown export.
- No change to the synchronous `write()` path or reprocess archive logic.
