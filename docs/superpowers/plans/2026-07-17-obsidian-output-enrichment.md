# Enriched Obsidian Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the per-meeting Markdown note a first-class Obsidian artefact at parity with the vault's Circleback notes: re-rendered after enrichment, with hierarchical-tag frontmatter, a dashboard-wired `## My Tasks` section, client-subfolder routing, and cross-links.

**Architecture:** A two-pass write. Pass 1 keeps the existing pre-enrichment write (`enriched: false`) so the note appears immediately. Pass 2, a new `_rerender_markdown` step at the tail of `PipelineRunner._post_process_async`, rebuilds the same file (`enriched: true`) from a structured `NoteContext` assembled from the DB and the intelligence repos, mirroring the existing `_append_notion_insights` precedent. Content is composed by a new pure `note_assembler` from the summary's narrative sections plus writer-computed sections; a pure `taxonomy` resolver maps a client/project to its curated vault folder and hierarchical tag.

**Tech Stack:** Python 3.12, dataclasses, PyYAML, python-slugify, pytest + pytest-asyncio, aiosqlite. macOS-only daemon.

## Global Constraints

- No em dashes anywhere in code, comments, or emitted note content. Use commas, colons, or restructure. (U+2014 must not appear.)
- United Kingdom English in all writer-authored strings.
- Every new frontmatter list (`attendees`, `tags`) MUST serialise as a YAML block list, never an inline flow list or quoted string. Verified by a round-trip test.
- `enriched: true` only after post-processing populated the note.
- Re-render and reprocess must be idempotent: update the existing note in place, keyed on the meeting id via the stored `markdown_path`. Never create a second file.
- Never overwrite a manually corrected value: the enriched writer does not overwrite `client`/`project` frontmatter when `meeting.assignment_source == 'manual'`; title precedence follows the existing `title_source`/`preserve_title` machinery.
- Preserve the atomic write pattern (temp file + `os.replace`). Preserve existing code comments unless the code they describe is removed.
- Never invent a `client/*` or `project/*` tag. Unknown client/project: leave the tag off, route to `Unsorted/`, and surface a `pipeline.warning`.
- Tests must never load real ML models or fire real network/LLM calls. Full suite target: `python3 -m pytest tests/ -v` (~1180 tests) and `ruff check src/ tests/` green per task.

## Confirmed data shapes (from the current code)

- `MeetingRecord` (src/db/repository.py): `id, title, started_at, duration_seconds, transcript_json, summary_markdown, tags (list), word_count, attendees_json (str), series_id, client_id, project_id, assignment_source, template_name, title_source, markdown_path`.
- `ClientProjectRepository.get_client(id) -> {name, description, aliases, email_domains, status, ...}`; `get_project(id) -> {name, client_id, description, aliases, ...}`.
- `ActionItemRepository.list_by_meeting(meeting_id) -> list[dict]` with `title, assignee, due_date, priority (low|medium|high|urgent), status (open|in_progress|done|cancelled), description, source, client_id, project_id`.
- `InsightRepository.results_for_meeting(meeting_id) -> list[{definition_id, definition_name, content, speaker, fields}]`. `render_insights_section(results)` consumes `definition_name` + `content`.
- `compute_talk_stats(transcript_json) -> {speakers: [{speaker, seconds, percent, turns, longest_monologue_seconds}], total_speaking_seconds}`.
- `SeriesRepository.get_meetings(series_id) -> list[dict]` (series siblings).
- `Transcript.segments[*]`: `.timestamp, .speaker, .text, .start, .end`; `Transcript.word_count`.

## File Structure

- Create `src/output/note_context.py`: `ActionItemView`, `NoteContext` dataclasses (pure data, imports only `Transcript`).
- Create `src/output/taxonomy.py`: `TaxonomyResolution`, `resolve_taxonomy()` (pure).
- Create `src/output/note_assembler.py`: section split/canonicalise, body assembly, table/callout/My-Tasks/transcript renderers (pure; imports `note_context`, `render_insights_section`, `compute_talk_stats`).
- Modify `src/output/markdown_writer.py`: add `write_note(ctx)`, block-list YAML dump, client-routed target path, atomic re-route; refactor `write()` to an adapter.
- Modify `src/pipeline_runner.py`: `_build_note_context`, `_rerender_markdown_async`, wire into `_post_process_async`; pass 1 unchanged content path.
- Modify `src/templates.py`: rename `standard` template headings to the gold set, split Open Questions and Risks.
- Modify `src/utils/config.py`: extend `MarkdownConfig`.
- Modify `config.example.yaml`: document new `markdown` fields + seed taxonomy.
- Tests under `tests/`: one module per task plus `tests/test_note_golden.py`.

---

## Phase 1: Re-render after enrichment (the enabling change)

### Task 1: `NoteContext` + `ActionItemView` data model

**Files:**

- Create: `src/output/note_context.py`
- Test: `tests/test_note_context.py`

**Interfaces:**

- Produces: `ActionItemView` and `NoteContext` dataclasses (field list below); `NoteContext.all_tags -> list[str]` property returning `extra_tags + client_tag + project_tag`, order-preserving, dropping empties and duplicates.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_note_context.py
from src.output.note_context import ActionItemView, NoteContext


def test_all_tags_orders_extra_then_client_then_project_dropping_empties():
    ctx = NoteContext(
        recall_id="m1", title="T", date="2026-07-15", time="10:03",
        started_at=0.0, duration_minutes=28, word_count=4456,
        client_tag="client/siemens", project_tag="project/siemens-16",
        extra_tags=["qvccs-internal"],
    )
    assert ctx.all_tags == ["qvccs-internal", "client/siemens", "project/siemens-16"]


def test_all_tags_dedupes_and_skips_blank():
    ctx = NoteContext(
        recall_id="m1", title="T", date="2026-07-15", time="10:03",
        started_at=0.0, duration_minutes=1, word_count=1,
        client_tag="", project_tag="project/x", extra_tags=["project/x", "topic"],
    )
    assert ctx.all_tags == ["project/x", "topic"]


def test_defaults_are_safe():
    ctx = NoteContext(
        recall_id="m1", title="T", date="2026-07-15", time="10:03",
        started_at=0.0, duration_minutes=1, word_count=1,
    )
    assert ctx.attendees == [] and ctx.action_items == [] and ctx.enriched is False
    assert ctx.client_folder == "Unsorted" and ctx.transcript_mode == "inline"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_note_context.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.output.note_context'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/output/note_context.py
"""Structured input for the enriched Markdown note writer.

Pure data. The pipeline assembles a NoteContext from the meeting row and
the intelligence repositories; the writer and assembler consume it instead
of scraping the summary's raw markdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.transcriber import Transcript


@dataclass
class ActionItemView:
    """One action item, flattened for rendering."""

    title: str
    assignee: str | None = None
    due_date: str | None = None       # ISO YYYY-MM-DD or None
    priority: str = "medium"          # low | medium | high | urgent
    status: str = "open"              # open | in_progress | done | cancelled
    description: str | None = None
    client_tag: str = ""              # "client/siemens" or ""
    project_tag: str = ""             # "project/siemens-16" or ""


@dataclass
class NoteContext:
    """Everything the writer needs to render one meeting note."""

    recall_id: str
    title: str
    date: str                         # YYYY-MM-DD
    time: str                         # HH:MM
    started_at: float
    duration_minutes: int
    word_count: int
    client_name: str = ""
    client_folder: str = "Unsorted"
    client_tag: str = ""              # "client/siemens" or ""
    project_name: str = ""
    project_tag: str = ""             # "project/siemens-16" or ""
    meeting_type: str = ""
    attendees: list[str] = field(default_factory=list)
    owner_display_name: str = "Jamie White (QVCCS)"
    extra_tags: list[str] = field(default_factory=list)
    summary_markdown: str = ""
    action_items: list[ActionItemView] = field(default_factory=list)
    owner_tasks: list[ActionItemView] = field(default_factory=list)
    insights: list[dict] = field(default_factory=list)
    talk_stats: dict = field(default_factory=dict)
    related_links: list[tuple[str, str]] = field(default_factory=list)  # (label, note_name)
    transcript: Transcript | None = None
    transcript_mode: str = "inline"
    enriched: bool = False

    @property
    def all_tags(self) -> list[str]:
        ordered = [*self.extra_tags, self.client_tag, self.project_tag]
        seen: set[str] = set()
        out: list[str] = []
        for tag in ordered:
            tag = (tag or "").strip()
            if tag and tag not in seen:
                seen.add(tag)
                out.append(tag)
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_note_context.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/output/note_context.py tests/test_note_context.py
git commit -m "feat(output): add NoteContext structured writer input"
```

---

### Task 2: `MarkdownWriter.write_note` reproducing current output; `write()` becomes an adapter

This is behaviour-preserving: `write_note` with a minimal context must emit the exact bytes the current `write()` emits, so nothing regresses before the enrichment phases.

**Files:**

- Modify: `src/output/markdown_writer.py`
- Test: `tests/test_markdown_writer_write_note.py`

**Interfaces:**

- Consumes: `NoteContext` (Task 1).
- Produces: `MarkdownWriter.write_note(ctx: NoteContext) -> Path | None`; `MarkdownWriter.write(summary, transcript, started_at, duration_seconds) -> Path | None` now delegates to `write_note` via `NoteContext.from_summary(...)` helper. Adds `MarkdownWriter._dump_frontmatter(fm: dict) -> str` and `MarkdownWriter._target_path(ctx) -> Path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_markdown_writer_write_note.py
import time
from pathlib import Path

import yaml

from src.output.markdown_writer import MarkdownWriter
from src.summariser import MeetingSummary
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _cfg(tmp_path: Path) -> MarkdownConfig:
    return MarkdownConfig(
        enabled=True, vault_path=str(tmp_path),
        filename_template="{date}_{slug}.md", include_full_transcript=False,
    )


def _transcript() -> Transcript:
    seg = TranscriptSegment(start=0.0, end=2.0, text="Hello there", speaker="Me", timestamp="00:00")
    return Transcript(segments=[seg], language="en", duration_seconds=2.0)


def test_write_via_summary_adapter_still_writes_note(tmp_path):
    w = MarkdownWriter(_cfg(tmp_path))
    summary = MeetingSummary(raw_markdown="# Daily Standup\n\n## Summary\n\nWe met.\n", title="Daily Standup", tags=["standup", "team"])
    path = w.write(summary, _transcript(), started_at=1_752_570_180.0, duration_seconds=1680.0)
    assert path is not None and path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    fm = yaml.safe_load(text.split("---\n")[1])
    assert fm["title"] == "Daily Standup"
    assert fm["tags"] == ["standup", "team"]
    assert "We met." in text
    assert "—" not in text  # no em dash in the footer


def test_write_note_returns_none_and_sets_error_on_unwritable_vault(tmp_path):
    cfg = _cfg(tmp_path / "file_not_dir")
    (tmp_path / "file_not_dir").write_text("x")  # a file where a dir is expected
    w = MarkdownWriter(cfg)
    summary = MeetingSummary(raw_markdown="# T\n", title="T", tags=[])
    path = w.write(summary, _transcript(), started_at=1_752_570_180.0, duration_seconds=60.0)
    assert path is None and w.last_error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_markdown_writer_write_note.py -v`
Expected: FAIL (the footer currently contains an em dash, and `write` does not yet delegate; the first test's `—` assertion fails).

- [ ] **Step 3: Write minimal implementation**

In `src/output/markdown_writer.py`: add a `NoteContext.from_summary` classmethod call path. Concretely, add these methods to `MarkdownWriter` and rewrite `write()` to build a context and delegate. Replace the footer em dash with a comma.

```python
# add imports at top
from src.output.note_context import NoteContext

# --- inside MarkdownWriter ---

def write(self, summary, transcript, started_at, duration_seconds):
    """Backwards-compatible pre-enrichment write.

    Builds a minimal NoteContext from the summary and delegates to
    write_note(). Kept so existing callers and tests are unaffected.
    """
    ctx = self._context_from_summary(summary, transcript, started_at, duration_seconds)
    return self.write_note(ctx)

def _context_from_summary(self, summary, transcript, started_at, duration_seconds) -> NoteContext:
    date_str = time.strftime("%Y-%m-%d", time.localtime(started_at))
    time_str = time.strftime("%H:%M", time.localtime(started_at))
    return NoteContext(
        recall_id="",
        title=summary.title,
        date=date_str,
        time=time_str,
        started_at=started_at,
        duration_minutes=int(duration_seconds / 60),
        word_count=transcript.word_count,
        extra_tags=list(summary.tags),
        summary_markdown=summary.raw_markdown,
        transcript=transcript,
        transcript_mode="inline" if self._config.include_full_transcript else "omit",
        enriched=False,
    )

def write_note(self, ctx: NoteContext) -> Path | None:
    """Write a note from a NoteContext. Atomic (temp + os.replace)."""
    self.last_error = None
    try:
        filepath = self._target_path(ctx)
    except ValueError:
        raise
    except OSError as e:
        self.last_error = f"Could not prepare vault path: {e}"
        logger.error("Markdown write failed: %s", self.last_error)
        return None

    frontmatter_yaml = self._dump_frontmatter(self._build_frontmatter(ctx))
    from src.output.note_assembler import assemble_body  # local import avoids cycle

    body = assemble_body(ctx)
    content = f"---\n{frontmatter_yaml}\n---\n\n{body}"

    tmp_path = filepath.with_name(filepath.name + ".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, filepath)
    except OSError as e:
        self.last_error = f"Could not write markdown file {filepath}: {e}"
        logger.error("Markdown write failed: %s", self.last_error)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    logger.info("Markdown written: %s", filepath)
    return filepath

def _build_frontmatter(self, ctx: NoteContext) -> dict:
    # Phase 1: reproduce the legacy frontmatter. Later phases extend this.
    return {
        "title": ctx.title,
        "date": ctx.date,
        "time": ctx.time,
        "duration_minutes": ctx.duration_minutes,
        "word_count": ctx.word_count,
        "tags": ctx.all_tags,
        "type": "meeting-note",
    }

def _dump_frontmatter(self, fm: dict) -> str:
    return _yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip()

def _target_path(self, ctx: NoteContext) -> Path:
    vault_path = Path(self._config.vault_path)
    os.makedirs(vault_path, exist_ok=True)
    date_str = ctx.date
    time_str = ctx.time.replace(":", "-")
    title_slug = slugify(ctx.title, max_length=60)
    filename = self._config.filename_template.format(
        date=date_str, time=time_str, slug=title_slug or "meeting"
    )
    filename = filename.replace("/", "_").replace("\\", "_").lstrip(".")
    filepath = (vault_path / filename).resolve()
    if not filepath.is_relative_to(vault_path.resolve()):
        raise ValueError(f"Generated filename would escape the vault directory: {filename!r}")
    return filepath
```

Delete the old body-building block in `write()` (the `content_parts` / transcript-inlining code) since `assemble_body` now owns it. The footer and transcript move into `note_assembler` in Task 3.

- [ ] **Step 4: Create the minimal `assemble_body` so the import resolves**

Create `src/output/note_assembler.py` with a Phase-1 body that reproduces current output (summary verbatim, comma-not-dash footer, inline transcript when mode is inline). This is fleshed out in Task 3; the minimal version:

```python
# src/output/note_assembler.py
"""Compose the Markdown note body from a NoteContext."""

from __future__ import annotations

import time

from src.output.note_context import NoteContext


def assemble_body(ctx: NoteContext) -> str:
    parts = [ctx.summary_markdown.rstrip(), "", "---", ""]
    parts.append(
        f"*Generated by Context Recall on {time.strftime('%Y-%m-%d %H:%M')}, "
        f"{ctx.duration_minutes} min, {ctx.word_count:,} words*"
    )
    if ctx.transcript_mode == "inline" and ctx.transcript is not None:
        parts += ["", "---", "", "## Full Transcript", ""]
        for seg in ctx.transcript.segments:
            speaker = f" *{seg.speaker}*:" if seg.speaker else ""
            parts.append(f"**{seg.timestamp}**{speaker} {seg.text.strip()}")
            parts.append("")
    return "\n".join(parts)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_markdown_writer_write_note.py tests/test_markdown_writer.py -v`
Expected: PASS. If any existing `tests/test_markdown_writer.py` case asserts the old em dash footer, update that assertion to the comma form (the em dash is banned by the spec).

- [ ] **Step 6: Commit**

```bash
git add src/output/markdown_writer.py src/output/note_assembler.py tests/test_markdown_writer_write_note.py tests/test_markdown_writer.py
git commit -m "refactor(output): route write() through write_note(NoteContext); drop footer em dash"
```

---

### Task 3: Pipeline re-render step (`_rerender_markdown_async`) with idempotency

**Files:**

- Modify: `src/pipeline_runner.py`
- Test: `tests/test_pipeline_rerender.py`

**Interfaces:**

- Consumes: `MarkdownWriter.write_note` (Task 2), `NoteContext` (Task 1).
- Produces: `PipelineRunner._build_note_context(meeting, transcript, *, enriched) -> NoteContext` and `async PipelineRunner._rerender_markdown_async(meeting_id) -> None`, invoked at the tail of `_post_process_async`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pipeline_rerender.py
import asyncio
from pathlib import Path

import pytest

from src.output.markdown_writer import MarkdownWriter
from src.pipeline_runner import PipelineRunner
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


class _Repo:
    def __init__(self, meeting):
        self._meeting = meeting
        self.updated = {}
    async def get_meeting(self, mid):
        return self._meeting
    async def update_meeting(self, mid, **fields):
        self.updated.update(fields)


class _FakeMeeting:
    id = "m1"
    title = "Daily Standup"
    started_at = 1_752_570_180.0
    duration_seconds = 1680.0
    transcript_json = '{"segments": [{"start": 0, "end": 2, "text": "hi", "speaker": "Me"}]}'
    summary_markdown = "# Daily Standup\n\n## Summary\n\nWe met.\n"
    tags = ["standup"]
    word_count = 4456
    attendees_json = "[]"
    series_id = None
    client_id = None
    project_id = None
    assignment_source = ""
    template_name = "standup"
    markdown_path = ""


@pytest.mark.asyncio
async def test_rerender_updates_same_file_and_sets_enriched(tmp_path, monkeypatch):
    # First write a pass-1 note so markdown_path is set.
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path),
                         filename_template="{date}_{slug}.md", include_full_transcript=False)
    writer = MarkdownWriter(cfg)
    seg = TranscriptSegment(start=0.0, end=2.0, text="hi", speaker="Me", timestamp="00:00")
    transcript = Transcript(segments=[seg], language="en", duration_seconds=2.0)

    meeting = _FakeMeeting()
    # Build a config object exposing .markdown for the runner.
    class _Cfg:
        markdown = cfg
    runner = PipelineRunner.__new__(PipelineRunner)
    runner._config = _Cfg()
    runner._md_writer = writer
    runner._emit_cb = None

    ctx = runner._build_note_context(meeting, transcript, enriched=False)
    first = writer.write_note(ctx)
    meeting.markdown_path = str(first)
    before = first.read_text(encoding="utf-8")
    assert "enriched: false" in before or "enriched" not in before

    # Re-render enriched=True must rewrite the SAME file.
    ctx2 = runner._build_note_context(meeting, transcript, enriched=True)
    second = writer.write_note(ctx2)
    assert second == first
    assert len(list(Path(tmp_path).glob("*.md"))) == 1
    assert "enriched: true" in second.read_text(encoding="utf-8")
```

Note: `enriched` frontmatter is added in Task 5; for Task 3 assert only single-file idempotency and same-path. Adjust the `enriched:` assertions to be introduced in Task 5. Keep the "one .md file" assertion here.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pipeline_rerender.py -v`
Expected: FAIL with `AttributeError: 'PipelineRunner' object has no attribute '_build_note_context'`.

- [ ] **Step 3: Write minimal implementation**

Add to `PipelineRunner` (src/pipeline_runner.py):

```python
import time as _time_module

def _build_note_context(self, meeting, transcript, *, enriched: bool):
    """Assemble a NoteContext from a meeting row + transcript.

    Phase 1 fills identity, timing, summary body and transcript. Later
    phases enrich taxonomy, attendees, action items, insights, talk stats
    and related links here.
    """
    from src.output.note_context import NoteContext

    started_at = getattr(meeting, "started_at", 0.0) or 0.0
    duration_seconds = getattr(meeting, "duration_seconds", 0.0) or 0.0
    date_str = _time_module.strftime("%Y-%m-%d", _time_module.localtime(started_at))
    time_str = _time_module.strftime("%H:%M", _time_module.localtime(started_at))
    md_cfg = self._config.markdown
    return NoteContext(
        recall_id=getattr(meeting, "id", "") or "",
        title=getattr(meeting, "title", "") or "Untitled Meeting",
        date=date_str,
        time=time_str,
        started_at=started_at,
        duration_minutes=int((duration_seconds or 0.0) / 60),
        word_count=getattr(meeting, "word_count", 0) or 0,
        extra_tags=list(getattr(meeting, "tags", []) or []),
        summary_markdown=getattr(meeting, "summary_markdown", "") or "",
        meeting_type=(getattr(meeting, "template_name", "") or "").capitalize(),
        transcript=transcript,
        transcript_mode=(
            getattr(md_cfg, "transcript_mode", None)
            or ("inline" if getattr(md_cfg, "include_full_transcript", False) else "omit")
        ),
        enriched=enriched,
    )

async def _rerender_markdown_async(self, meeting_id: str) -> None:
    """Pass 2: rewrite the note in place with post-processing data.

    Located by the meeting's stored markdown_path so a re-run updates the
    same file rather than duplicating it (idempotent, reprocess-safe).
    """
    if not getattr(self._config.markdown, "enabled", False) or self._md_writer is None:
        return
    if self._db.database is None:
        return
    meeting = await self._db.repo.get_meeting(meeting_id)
    if meeting is None:
        return
    transcript = None
    if getattr(meeting, "transcript_json", None):
        from src.transcriber import Transcript
        try:
            transcript = Transcript.from_dict(json.loads(meeting.transcript_json))
        except Exception:
            transcript = None
    ctx = await self._augment_note_context(self._build_note_context(meeting, transcript, enriched=True), meeting)
    # Reuse the existing note path so the write lands on the same file.
    existing = getattr(meeting, "markdown_path", "") or ""
    if existing:
        self._md_writer.reuse_path(Path(existing))
    new_path = await asyncio.to_thread(self._md_writer.write_note, ctx)
    if new_path is not None and str(new_path) != existing:
        await self._db.repo.update_meeting(meeting_id, markdown_path=str(new_path))
```

Add `_augment_note_context` as a no-op passthrough for Phase 1 (later phases fill it):

```python
async def _augment_note_context(self, ctx, meeting):
    """Enrich a NoteContext with DB-derived data. Phase 1: identity only."""
    return ctx
```

Add a `reuse_path` hook to `MarkdownWriter` so re-render targets the exact existing file (bypasses filename recomputation, which matters once routing/renames exist):

```python
# in MarkdownWriter.__init__: self._reuse_path: Path | None = None
def reuse_path(self, path: Path) -> None:
    """Force the next write_note() to target this exact path (re-render)."""
    self._reuse_path = Path(path)
```

And in `_target_path`, honour it (then clear it):

```python
def _target_path(self, ctx):
    if self._reuse_path is not None:
        target = self._reuse_path
        self._reuse_path = None
        os.makedirs(target.parent, exist_ok=True)
        return target
    ...  # existing computation
```

Finally, wire the call at the end of `_post_process_async` (after `_refresh_analytics`):

```python
try:
    await self._rerender_markdown_async(meeting_id)
except Exception:
    logger.warning("Markdown re-render failed", exc_info=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pipeline_rerender.py -v`
Expected: PASS (one .md file; second write returns the same path).

- [ ] **Step 5: Run the pipeline suite to check no regression**

Run: `python3 -m pytest tests/test_pipeline_runner.py tests/test_reprocess*.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pipeline_runner.py src/output/markdown_writer.py tests/test_pipeline_rerender.py
git commit -m "feat(pipeline): re-render markdown after enrichment, idempotent on markdown_path"
```

---

## Phase 2: Frontmatter parity and hierarchical tags

### Task 4: Taxonomy resolver

**Files:**

- Create: `src/output/taxonomy.py`
- Test: `tests/test_taxonomy.py`

**Interfaces:**

- Produces: `TaxonomyResolution(client_folder, client_tag, project_tag, unknown_client, unknown_project)`; `resolve_taxonomy(client_name, project_name, taxonomy, *, fallback_folder="Unsorted") -> TaxonomyResolution`. `taxonomy` is the `MarkdownConfig.client_taxonomy` dict: `{"clients": {name: {"folder": str, "tag": str}}, "projects": {name: str}}`. Matching is case-insensitive on name.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_taxonomy.py
from src.output.taxonomy import resolve_taxonomy

TAX = {
    "clients": {
        "Siemens": {"folder": "Siemens", "tag": "client/siemens"},
        "QVCCS Internal": {"folder": "QVCCS Internal", "tag": "qvccs-internal"},
    },
    "projects": {"Siemens 16 Smart UK Infrastructure": "project/siemens-16"},
}


def test_known_client_and_project():
    r = resolve_taxonomy("Siemens", "Siemens 16 Smart UK Infrastructure", TAX)
    assert r.client_folder == "Siemens"
    assert r.client_tag == "client/siemens"
    assert r.project_tag == "project/siemens-16"
    assert not r.unknown_client and not r.unknown_project


def test_case_insensitive_client_match():
    assert resolve_taxonomy("siemens", "", TAX).client_tag == "client/siemens"


def test_unknown_client_falls_back_and_flags():
    r = resolve_taxonomy("Acme Corp", "", TAX)
    assert r.client_folder == "Unsorted"
    assert r.client_tag == ""
    assert r.unknown_client is True


def test_flat_client_tag_is_preserved():
    # QVCCS Internal maps to a flat tag, not client/*
    r = resolve_taxonomy("QVCCS Internal", "", TAX)
    assert r.client_folder == "QVCCS Internal" and r.client_tag == "qvccs-internal"


def test_unknown_project_flags_but_client_still_resolves():
    r = resolve_taxonomy("Siemens", "Mystery Project", TAX)
    assert r.client_tag == "client/siemens"
    assert r.project_tag == "" and r.unknown_project is True


def test_empty_names_resolve_to_unknown_without_error():
    r = resolve_taxonomy("", "", TAX)
    assert r.client_folder == "Unsorted" and r.client_tag == "" and r.project_tag == ""
    assert r.unknown_client is False and r.unknown_project is False  # empty != unknown
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_taxonomy.py -v`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/output/taxonomy.py
"""Resolve a client/project to its curated vault folder and tag.

The vault uses curated short tag slugs (client/siemens, project/siemens-16)
that do not match slugify() of the full names, so the mapping is an explicit
config map, never derived. Unknown clients or projects are flagged so the
caller can surface a warning rather than fabricate a tag.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TaxonomyResolution:
    client_folder: str
    client_tag: str
    project_tag: str
    unknown_client: bool
    unknown_project: bool


def _lookup_ci(mapping: dict, name: str):
    if not name:
        return None, False
    for key, value in (mapping or {}).items():
        if key.strip().lower() == name.strip().lower():
            return value, True
    return None, False


def resolve_taxonomy(
    client_name: str,
    project_name: str,
    taxonomy: dict,
    *,
    fallback_folder: str = "Unsorted",
) -> TaxonomyResolution:
    clients = (taxonomy or {}).get("clients", {})
    projects = (taxonomy or {}).get("projects", {})

    client_entry, client_found = _lookup_ci(clients, client_name)
    folder = fallback_folder
    client_tag = ""
    if client_found and isinstance(client_entry, dict):
        folder = client_entry.get("folder") or fallback_folder
        client_tag = client_entry.get("tag") or ""

    project_entry, project_found = _lookup_ci(projects, project_name)
    project_tag = project_entry if (project_found and isinstance(project_entry, str)) else ""

    return TaxonomyResolution(
        client_folder=folder,
        client_tag=client_tag,
        project_tag=project_tag,
        unknown_client=bool(client_name) and not client_found,
        unknown_project=bool(project_name) and not project_found,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_taxonomy.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/output/taxonomy.py tests/test_taxonomy.py
git commit -m "feat(output): curated client/project taxonomy resolver"
```

---

### Task 5: Block-list frontmatter + enriched fields + attendee folding

**Files:**

- Modify: `src/output/markdown_writer.py` (`_build_frontmatter`, `_dump_frontmatter`)
- Create: `src/output/attendees.py` (owner folding + org suffix)
- Test: `tests/test_frontmatter_parity.py`, `tests/test_attendees.py`

**Interfaces:**

- Consumes: `NoteContext.all_tags`, taxonomy fields already on the context.
- Produces: `fold_attendees(names, owner_identities, owner_display_name) -> list[str]` in `src/output/attendees.py`; extended `_build_frontmatter` emitting `title, date, time, client, project, meeting_type, duration_minutes, word_count, attendees, tags, source, recall_id, enriched`; `_dump_frontmatter` guaranteeing block-list serialisation for `attendees` and `tags`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_attendees.py
from src.output.attendees import fold_attendees


def test_folds_owner_identities_to_display_name_and_dedupes():
    out = fold_attendees(
        ["Me", "jamiecs@live.co.uk", "Amelia Lawton"],
        owner_identities=["me", "jamie", "jamiecs@live.co.uk", "j65541761@gmail.com"],
        owner_display_name="Jamie White (QVCCS)",
    )
    assert out == ["Jamie White (QVCCS)", "Amelia Lawton"]


def test_leaves_unknown_labels_untouched():
    out = fold_attendees(["SPEAKER_01", "Remote"], owner_identities=["me"], owner_display_name="Jamie White (QVCCS)")
    assert out == ["SPEAKER_01", "Remote"]


def test_owner_first_when_present():
    out = fold_attendees(["Amelia Lawton", "Jamie"], owner_identities=["jamie"], owner_display_name="Jamie White (QVCCS)")
    assert out[0] == "Jamie White (QVCCS)"
```

```python
# tests/test_frontmatter_parity.py
import yaml

from src.output.markdown_writer import MarkdownWriter
from src.output.note_context import NoteContext
from src.transcriber import Transcript
from src.utils.config import MarkdownConfig


def _ctx(**kw):
    base = dict(
        recall_id="4f2a", title="Morning Standup Call", date="2026-07-15", time="10:03",
        started_at=1_752_570_180.0, duration_minutes=28, word_count=4456,
        client_name="QVCCS Internal", client_folder="QVCCS Internal", client_tag="qvccs-internal",
        project_name="", project_tag="", meeting_type="Standup",
        attendees=["Jamie White (QVCCS)", "Amelia Lawton (QVCCS)"],
        extra_tags=[], enriched=True,
    )
    base.update(kw)
    return NoteContext(**base)


def test_attendees_and_tags_round_trip_as_block_lists(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), filename_template="{date}_{slug}.md")
    w = MarkdownWriter(cfg)
    ctx = _ctx(project_tag="project/siemens-16", extra_tags=["qvccs-internal"])
    path = w.write_note(ctx)
    text = path.read_text(encoding="utf-8")
    # Block-list form, never inline flow list.
    assert "attendees:\n  - Jamie White (QVCCS)\n  - Amelia Lawton (QVCCS)" in text
    assert "tags:\n  - qvccs-internal\n  - project/siemens-16" in text
    fm = yaml.safe_load(text.split("---\n")[1])
    assert isinstance(fm["attendees"], list) and isinstance(fm["tags"], list)
    assert fm["client"] == "QVCCS Internal" and fm["source"] == "context-recall"
    assert fm["recall_id"] == "4f2a" and fm["enriched"] is True
    assert fm["meeting_type"] == "Standup"


def test_time_is_quoted_string(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), filename_template="{date}_{slug}.md")
    path = MarkdownWriter(cfg).write_note(_ctx())
    assert 'time: "10:03"' in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_attendees.py tests/test_frontmatter_parity.py -v`
Expected: FAIL (`attendees` module missing; frontmatter lacks the new keys).

- [ ] **Step 3: Write the implementation**

`src/output/attendees.py`:

```python
# src/output/attendees.py
"""Resolve and fold attendee display names for the note."""

from __future__ import annotations


def fold_attendees(
    names: list[str],
    owner_identities: list[str],
    owner_display_name: str,
) -> list[str]:
    """Fold owner identities to the owner's display name, keep order, dedupe.

    A label matches an owner identity case-insensitively. Unknown labels are
    passed through unchanged (never guessed). The owner, when present, is
    listed first.
    """
    ident = {i.strip().lower() for i in owner_identities if i and i.strip()}
    owner_present = False
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        clean = (name or "").strip()
        if not clean:
            continue
        if clean.lower() in ident:
            owner_present = True
            continue
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    if owner_present:
        return [owner_display_name, *[n for n in out if n != owner_display_name]]
    return out
```

Extend `_build_frontmatter` in `markdown_writer.py`:

```python
def _build_frontmatter(self, ctx: NoteContext) -> dict:
    fm: dict = {
        "title": ctx.title,
        "date": ctx.date,
        "time": ctx.time,
    }
    if ctx.enriched:
        fm["client"] = ctx.client_name
        fm["project"] = ctx.project_name
        fm["meeting_type"] = ctx.meeting_type
    fm["duration_minutes"] = ctx.duration_minutes
    fm["word_count"] = ctx.word_count
    if ctx.enriched:
        fm["attendees"] = list(ctx.attendees)
    fm["tags"] = ctx.all_tags
    if ctx.enriched:
        fm["source"] = "context-recall"
        fm["recall_id"] = ctx.recall_id
        fm["enriched"] = True
    else:
        fm["type"] = "meeting-note"
    return fm
```

Rewrite `_dump_frontmatter` to force block-list style and quote `time`:

```python
def _dump_frontmatter(self, fm: dict) -> str:
    # default_flow_style=False already renders lists as block lists; the
    # explicit test guards against a future regression to flow style. Quote
    # time so YAML does not coerce "10:03" to a sexagesimal integer.
    dumped = _yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False, width=1000
    ).rstrip()
    return dumped
```

Note: PyYAML renders `"10:03"` quoted automatically because it would otherwise parse as a time; verify in the test. If a plain `10:03` slips through, set `fm["time"]` via a `yaml`-safe quoted scalar. The `test_time_is_quoted_string` test is the guard.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_attendees.py tests/test_frontmatter_parity.py -v`
Expected: PASS.

- [ ] **Step 5: Wire taxonomy + attendees + enriched frontmatter into `_augment_note_context`**

In `src/pipeline_runner.py`, flesh out `_augment_note_context` (replacing the Phase 1 passthrough):

```python
async def _augment_note_context(self, ctx, meeting):
    """Enrich a NoteContext with DB-derived taxonomy, attendees and tags."""
    from src.output.attendees import fold_attendees
    from src.output.taxonomy import resolve_taxonomy
    from src.tagging.repository import ClientProjectRepository

    md_cfg = self._config.markdown
    client_name = ""
    project_name = ""
    if self._db.database is not None and (meeting.client_id or meeting.project_id):
        cp = ClientProjectRepository(self._db.database)
        if meeting.client_id:
            client = await cp.get_client(meeting.client_id)
            client_name = (client or {}).get("name", "") if client else ""
        if meeting.project_id:
            project = await cp.get_project(meeting.project_id)
            project_name = (project or {}).get("name", "") if project else ""

    resolution = resolve_taxonomy(client_name, project_name, getattr(md_cfg, "client_taxonomy", {}) or {})
    if resolution.unknown_client:
        self._emit("pipeline.warning", meeting_id=meeting.id, source="markdown",
                   message=f"Client '{client_name}' has no vault taxonomy entry; note routed to Unsorted and left untagged.")
    if resolution.unknown_project:
        self._emit("pipeline.warning", meeting_id=meeting.id, source="markdown",
                   message=f"Project '{project_name}' has no vault taxonomy entry; left untagged.")

    ctx.client_name = client_name
    ctx.project_name = project_name
    ctx.client_folder = resolution.client_folder
    ctx.client_tag = resolution.client_tag
    ctx.project_tag = resolution.project_tag

    # Attendees: calendar attendee names UNION resolved transcript speakers.
    try:
        raw = json.loads(meeting.attendees_json or "[]")
    except (ValueError, TypeError):
        raw = []
    names = [a.get("name", "") for a in raw if isinstance(a, dict) and a.get("name")]
    if ctx.transcript is not None:
        names += [s for s in {seg.speaker for seg in ctx.transcript.segments} if s]
    ctx.owner_display_name = getattr(md_cfg, "owner_display_name", "Jamie White (QVCCS)")
    ctx.attendees = fold_attendees(
        names,
        getattr(md_cfg, "owner_identities", []) or [],
        ctx.owner_display_name,
    )
    # Topic tags from the summary's freeform tags, excluding any client/project noise.
    ctx.extra_tags = [t for t in (getattr(meeting, "tags", []) or []) if not t.startswith(("client/", "project/"))]
    return ctx
```

- [ ] **Step 6: Run the pipeline + writer suites**

Run: `python3 -m pytest tests/test_pipeline_rerender.py tests/test_markdown_writer_write_note.py -v && ruff check src/`
Expected: PASS, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add src/output/attendees.py src/output/markdown_writer.py src/pipeline_runner.py tests/test_attendees.py tests/test_frontmatter_parity.py
git commit -m "feat(output): enriched block-list frontmatter, taxonomy tags, attendee folding"
```

---

## Phase 3: `## My Tasks` wired into the dashboard

### Task 6: My Tasks line formatter + owner filtering

**Files:**

- Modify: `src/output/note_assembler.py` (`format_my_task`, `render_my_tasks`)
- Modify: `src/pipeline_runner.py` (`_augment_note_context` fills `action_items` + `owner_tasks`)
- Test: `tests/test_my_tasks.py`

**Interfaces:**

- Consumes: `ActionItemView` (Task 1).
- Produces: `format_my_task(item: ActionItemView) -> str`; `render_my_tasks(items: list[ActionItemView]) -> str` (returns `""` when empty); `select_owner_tasks(items, owner_identities, owner_display_name) -> list[ActionItemView]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_my_tasks.py
from src.output.note_assembler import format_my_task, render_my_tasks, select_owner_tasks
from src.output.note_context import ActionItemView


def test_line_format_medium_default_with_tags_and_date():
    item = ActionItemView(title="Rebuild the callback logic", priority="medium",
                          due_date="2026-07-18", client_tag="client/siemens", project_tag="project/siemens-16")
    line = format_my_task(item)
    assert line == "- [ ] Rebuild the callback logic #client/siemens #project/siemens-16 🔼 📅 2026-07-18"


def test_priority_emoji_mapping():
    assert "🔺" in format_my_task(ActionItemView(title="x", priority="urgent"))
    assert "⏫" in format_my_task(ActionItemView(title="x", priority="high"))
    assert "🔼" in format_my_task(ActionItemView(title="x", priority="low")) is False
    assert "🔽" in format_my_task(ActionItemView(title="x", priority="low"))


def test_no_date_when_missing():
    line = format_my_task(ActionItemView(title="x", priority="medium", project_tag="project/y"))
    assert "📅" not in line and line.endswith("🔼")


def test_render_my_tasks_matches_dashboard_query():
    items = [ActionItemView(title="Do X", project_tag="project/siemens-16")]
    section = render_my_tasks(items)
    assert section.startswith("## My Tasks")
    assert "- [ ] Do X" in section and "#project/siemens-16" in section  # dashboard query needs #project/


def test_render_empty_returns_blank():
    assert render_my_tasks([]) == ""


def test_select_owner_tasks_filters_incomplete_owner_items():
    items = [
        ActionItemView(title="mine open", assignee="Jamie", status="open"),
        ActionItemView(title="mine done", assignee="Me", status="done"),
        ActionItemView(title="theirs", assignee="Amelia", status="open"),
        ActionItemView(title="unassigned", assignee=None, status="open"),
    ]
    picked = select_owner_tasks(items, owner_identities=["me", "jamie"], owner_display_name="Jamie White (QVCCS)")
    assert [i.title for i in picked] == ["mine open"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_my_tasks.py -v`
Expected: FAIL (`ImportError: cannot import name 'format_my_task'`).

- [ ] **Step 3: Write the implementation**

Add to `src/output/note_assembler.py`:

```python
_PRIORITY_EMOJI = {"urgent": "🔺", "high": "⏫", "medium": "🔼", "low": "🔽"}
_INCOMPLETE = {"open", "in_progress"}


def format_my_task(item) -> str:
    """Render one owner task as a Tasks-plugin checkbox line.

    Format: - [ ] <title> [#client/x] [#project/y] <emoji> [📅 due]
    Tags carry the client/project so the Meeting Action Items dashboard
    query (contains "#client/" or "#project/") matches.
    """
    parts = [f"- [ ] {item.title.strip()}"]
    if item.client_tag:
        parts.append(f"#{item.client_tag}")
    if item.project_tag:
        parts.append(f"#{item.project_tag}")
    parts.append(_PRIORITY_EMOJI.get(item.priority, "🔼"))
    if item.due_date:
        parts.append(f"📅 {item.due_date}")
    return " ".join(parts)


def render_my_tasks(items) -> str:
    if not items:
        return ""
    lines = ["## My Tasks", ""]
    lines += [format_my_task(i) for i in items]
    return "\n".join(lines)


def select_owner_tasks(items, owner_identities, owner_display_name) -> list:
    ident = {i.strip().lower() for i in owner_identities if i and i.strip()}
    ident.add((owner_display_name or "").strip().lower())
    out = []
    for item in items:
        assignee = (item.assignee or "").strip().lower()
        if assignee in ident and item.status in _INCOMPLETE:
            out.append(item)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_my_tasks.py -v`
Expected: PASS.

- [ ] **Step 5: Populate action items in `_augment_note_context`**

Add (before the `return ctx`), building `ActionItemView`s and owner tasks. Note each item's tags come from the item's own client_id/project_id resolved through the SAME taxonomy (fall back to the meeting's tags when the item is unassigned so a My Task still carries the meeting's project tag):

```python
    from src.action_items.repository import ActionItemRepository
    from src.output.note_assembler import select_owner_tasks
    from src.output.note_context import ActionItemView

    if self._db.database is not None:
        ai_repo = ActionItemRepository(self._db.database)
        rows = await ai_repo.list_by_meeting(meeting.id)
        views = []
        for r in rows:
            # Item tags: prefer the item's own client/project, else inherit the note's.
            item_client_tag = ctx.client_tag
            item_project_tag = ctx.project_tag
            views.append(ActionItemView(
                title=r["title"], assignee=r.get("assignee"), due_date=r.get("due_date"),
                priority=r.get("priority", "medium"), status=r.get("status", "open"),
                description=r.get("description"),
                client_tag=item_client_tag, project_tag=item_project_tag,
            ))
        ctx.action_items = views
        ctx.owner_tasks = select_owner_tasks(
            views, getattr(md_cfg, "owner_identities", []) or [], ctx.owner_display_name
        )
```

- [ ] **Step 6: Commit**

```bash
git add src/output/note_assembler.py src/pipeline_runner.py tests/test_my_tasks.py
git commit -m "feat(output): My Tasks section wired to the dashboard tag query"
```

---

## Phase 4: Client subfolder routing and transcript handling

### Task 7: Transcript modes

**Files:**

- Modify: `src/output/note_assembler.py` (`render_transcript`)
- Test: `tests/test_transcript_modes.py`

**Interfaces:**

- Produces: `render_transcript(transcript, mode) -> str` returning the transcript block for `inline` / `foldout`, `""` for `omit`. `linked` returns `""` here (the companion note + link is handled by the writer in Task 8, which knows the vault path). The assembler receives the resolved wikilink via `ctx` for `linked`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_transcript_modes.py
from src.output.note_assembler import render_transcript
from src.transcriber import Transcript, TranscriptSegment


def _t():
    return Transcript(segments=[TranscriptSegment(start=0, end=2, text="Hello", speaker="Me", timestamp="00:00")],
                      language="en", duration_seconds=2.0)


def test_inline_lists_segments():
    out = render_transcript(_t(), "inline")
    assert "## Full Transcript" in out and "**00:00** *Me*: Hello" in out


def test_foldout_wraps_in_quote_callout():
    out = render_transcript(_t(), "foldout")
    assert out.startswith("> [!quote]- Full transcript")
    assert "> **00:00** *Me*: Hello" in out


def test_omit_returns_empty():
    assert render_transcript(_t(), "omit") == ""


def test_linked_returns_empty_here():
    assert render_transcript(_t(), "linked") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_transcript_modes.py -v`
Expected: FAIL (`render_transcript` not defined).

- [ ] **Step 3: Write the implementation**

```python
def render_transcript(transcript, mode: str) -> str:
    if transcript is None or mode in ("omit", "linked"):
        return ""
    rows = []
    for seg in transcript.segments:
        speaker = f" *{seg.speaker}*:" if seg.speaker else ""
        rows.append(f"**{seg.timestamp}**{speaker} {seg.text.strip()}")
    if mode == "foldout":
        body = "\n".join(f"> {r}" for r in rows)
        return "> [!quote]- Full transcript\n" + body
    # inline
    return "## Full Transcript\n\n" + "\n\n".join(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_transcript_modes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/output/note_assembler.py tests/test_transcript_modes.py
git commit -m "feat(output): transcript render modes (inline, foldout, omit, linked)"
```

---

### Task 8: Client-subfolder routing + companion transcript note + atomic re-route

**Files:**

- Modify: `src/output/markdown_writer.py` (`_target_path` honours `client_folder` + `route_by_client`; `linked` companion note; move-on-reroute)
- Test: `tests/test_client_routing.py`

**Interfaces:**

- Consumes: `NoteContext.client_folder`, `NoteContext.transcript_mode`, `MarkdownConfig.route_by_client`.
- Produces: `write_note` files into `<vault>/<client_folder>/<filename>` when `route_by_client`; when re-render's `reuse_path` points at a different folder than the resolved one, the note is moved atomically and the old file removed; `linked` mode writes `<name> (transcript).md` beside the note and the body links it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_client_routing.py
from pathlib import Path

from src.output.markdown_writer import MarkdownWriter
from src.output.note_context import NoteContext
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _ctx(**kw):
    base = dict(recall_id="m1", title="Weekly Review", date="2026-07-15", time="10:03",
                started_at=1_752_570_180.0, duration_minutes=28, word_count=10,
                client_folder="Siemens", enriched=True)
    base.update(kw)
    return NoteContext(**base)


def test_routes_into_client_folder(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=True,
                         filename_template="{date}_{slug}.md")
    path = MarkdownWriter(cfg).write_note(_ctx())
    assert path.parent.name == "Siemens" and path.exists()


def test_unknown_client_routes_to_unsorted(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=True,
                         filename_template="{date}_{slug}.md")
    path = MarkdownWriter(cfg).write_note(_ctx(client_folder="Unsorted"))
    assert path.parent.name == "Unsorted"


def test_route_disabled_writes_flat(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=False,
                         filename_template="{date}_{slug}.md")
    path = MarkdownWriter(cfg).write_note(_ctx())
    assert path.parent == Path(tmp_path)


def test_reroute_moves_existing_note_no_duplicate(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=True,
                         filename_template="{date}_{slug}.md")
    w = MarkdownWriter(cfg)
    first = w.write_note(_ctx(client_folder="Unsorted"))       # pass 1, unknown client
    w.reuse_path(first)
    second = w.write_note(_ctx(client_folder="Siemens"))       # re-render, now resolved
    assert second.parent.name == "Siemens"
    assert not first.exists()                                   # moved, not duplicated
    all_md = list(Path(tmp_path).rglob("*.md"))
    assert len(all_md) == 1


def test_linked_transcript_writes_companion(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=True,
                         filename_template="{date}_{slug}.md")
    seg = TranscriptSegment(start=0, end=2, text="Hi", speaker="Me", timestamp="00:00")
    ctx = _ctx(transcript=Transcript(segments=[seg], language="en", duration_seconds=2.0), transcript_mode="linked")
    path = MarkdownWriter(cfg).write_note(ctx)
    companions = list(path.parent.glob("*(transcript).md"))
    assert companions and "Hi" in companions[0].read_text(encoding="utf-8")
    assert f"[[{companions[0].stem}]]" in path.read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_client_routing.py -v`
Expected: FAIL (routing not implemented; `route_by_client` not on `MarkdownConfig` yet, so this also depends on Task 12's config field. To keep this task self-contained, add the `route_by_client`, `transcript_mode`, `owner_identities`, `owner_display_name`, `client_taxonomy` fields to `MarkdownConfig` in THIS task's Step 3 as well, with defaults; Task 12 only documents them in config.example.yaml.)

- [ ] **Step 3: Write the implementation**

First extend `MarkdownConfig` (src/utils/config.py) with defaults (documented later in Task 12):

```python
@dataclass
class MarkdownConfig:
    enabled: bool = True
    vault_path: str = "~/Documents/Meetings"
    filename_template: str = "{date}_{slug}.md"
    include_full_transcript: bool = True
    route_by_client: bool = True
    transcript_mode: str = "foldout"     # foldout | linked | omit | inline
    emit_my_tasks: bool = True
    owner_display_name: str = "Jamie White (QVCCS)"
    owner_identities: list[str] = field(default_factory=lambda: ["Me", "Jamie"])
    client_taxonomy: dict = field(default_factory=dict)
```

Update `_target_path` to route by client folder, and add re-route + companion logic in `write_note`:

```python
def _target_path(self, ctx: NoteContext) -> Path:
    vault_path = Path(self._config.vault_path)
    if self._reuse_path is not None and not getattr(self._config, "route_by_client", False):
        # Non-routing re-render: keep the exact file.
        target = self._reuse_path
        self._reuse_path = None
        os.makedirs(target.parent, exist_ok=True)
        return target
    base = vault_path
    if getattr(self._config, "route_by_client", False) and ctx.client_folder:
        base = vault_path / ctx.client_folder
    os.makedirs(base, exist_ok=True)
    time_str = ctx.time.replace(":", "-")
    title_slug = slugify(ctx.title, max_length=60)
    filename = self._config.filename_template.format(date=ctx.date, time=time_str, slug=title_slug or "meeting")
    filename = filename.replace("/", "_").replace("\\", "_").lstrip(".")
    filepath = (base / filename).resolve()
    if not filepath.is_relative_to(vault_path.resolve()):
        raise ValueError(f"Generated filename would escape the vault directory: {filename!r}")
    return filepath
```

In `write_note`, capture the previous path for a move and handle `linked`:

```python
def write_note(self, ctx: NoteContext) -> Path | None:
    self.last_error = None
    previous = self._reuse_path  # may be None
    try:
        filepath = self._target_path(ctx)
    except ValueError:
        raise
    ...
    # linked transcript: write companion note first, capture its stem for the body.
    ctx_link = None
    if ctx.transcript_mode == "linked" and ctx.transcript is not None:
        companion = filepath.with_name(f"{filepath.stem} (transcript){filepath.suffix}")
        from src.output.note_assembler import render_transcript
        rows = render_transcript(ctx.transcript, "inline")
        try:
            companion.write_text(f"# {ctx.title} (transcript)\n\n{rows}\n", encoding="utf-8")
            ctx_link = companion.stem
        except OSError as e:
            logger.warning("Could not write transcript companion note: %s", e)
    ...
    # after os.replace succeeds, move-on-reroute:
    if previous is not None and Path(previous).resolve() != filepath and Path(previous).exists():
        try:
            Path(previous).unlink()
        except OSError as e:
            logger.warning("Re-render wrote %s but could not remove old note %s: %s", filepath, previous, e)
    return filepath
```

Pass `ctx_link` into `assemble_body` so `linked` mode emits `- [[<companion stem>]]` under a `## Transcript` line. Extend `assemble_body` signature to `assemble_body(ctx, transcript_link: str | None = None)` and, when `ctx.transcript_mode == "linked"` and a link exists, append `## Transcript\n\n- [[<link>]]`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_client_routing.py tests/test_transcript_modes.py -v`
Expected: PASS.

- [ ] **Step 5: Run full writer + pipeline suites**

Run: `python3 -m pytest tests/test_markdown_writer_write_note.py tests/test_frontmatter_parity.py tests/test_pipeline_rerender.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/output/markdown_writer.py src/output/note_assembler.py src/utils/config.py tests/test_client_routing.py
git commit -m "feat(output): client-subfolder routing, atomic re-route, linked transcript note"
```

---

## Phase 5: Cross-linking and visual polish

### Task 9: Related links (series previous instance + project note, existing-only)

**Files:**

- Create: `src/output/related.py`
- Modify: `src/pipeline_runner.py` (fill `ctx.related_links`)
- Test: `tests/test_related_links.py`

**Interfaces:**

- Produces: `resolve_related(*, series_meetings, this_started_at, project_note_name, vault_base, client_folder) -> list[tuple[str, str]]`. Returns `("Previous", note_stem)` for the most recent series sibling with an earlier `started_at` whose `markdown_path` file exists, and `("Project", project_note_name)` when a note with that name exists anywhere under `vault_base`. Only existing notes are linked.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_related_links.py
from pathlib import Path

from src.output.related import resolve_related


def test_previous_instance_from_series(tmp_path):
    prev = tmp_path / "Siemens" / "2026-07-08 - prev.md"
    prev.parent.mkdir(parents=True)
    prev.write_text("x")
    series = [
        {"id": "a", "started_at": 100.0, "markdown_path": str(prev)},
        {"id": "b", "started_at": 200.0, "markdown_path": ""},  # this meeting
    ]
    out = resolve_related(series_meetings=series, this_started_at=200.0,
                          project_note_name="", vault_base=str(tmp_path), client_folder="Siemens")
    assert ("Previous", "2026-07-08 - prev") in out


def test_project_link_only_when_note_exists(tmp_path):
    (tmp_path / "10 Projects").mkdir(parents=True)
    (tmp_path / "10 Projects" / "Project Siemens 16.md").write_text("x")
    out = resolve_related(series_meetings=[], this_started_at=0.0,
                          project_note_name="Project Siemens 16", vault_base=str(tmp_path), client_folder="Siemens")
    assert ("Project", "Project Siemens 16") in out


def test_missing_project_note_is_not_linked(tmp_path):
    out = resolve_related(series_meetings=[], this_started_at=0.0,
                          project_note_name="Nonexistent", vault_base=str(tmp_path), client_folder="Siemens")
    assert out == []


def test_no_earlier_sibling_returns_no_previous(tmp_path):
    out = resolve_related(series_meetings=[{"id": "b", "started_at": 200.0, "markdown_path": ""}],
                          this_started_at=200.0, project_note_name="", vault_base=str(tmp_path), client_folder="Siemens")
    assert out == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_related_links.py -v`
Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/output/related.py
"""Resolve ## Related wikilinks to notes that actually exist."""

from __future__ import annotations

from pathlib import Path


def resolve_related(
    *,
    series_meetings: list[dict],
    this_started_at: float,
    project_note_name: str,
    vault_base: str,
    client_folder: str,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    # Previous instance: most recent earlier series sibling with an existing note.
    earlier = [
        m for m in (series_meetings or [])
        if (m.get("started_at") or 0.0) < this_started_at and (m.get("markdown_path") or "")
    ]
    earlier.sort(key=lambda m: m.get("started_at") or 0.0, reverse=True)
    for m in earlier:
        p = Path(m["markdown_path"])
        if p.exists():
            out.append(("Previous", p.stem))
            break
    # Project note: link only if a note with that name exists under the vault.
    if project_note_name:
        base = Path(vault_base)
        matches = list(base.rglob(f"{project_note_name}.md"))
        if matches:
            out.append(("Project", project_note_name))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_related_links.py -v`
Expected: PASS.

- [ ] **Step 5: Fill `ctx.related_links` in `_augment_note_context`**

```python
    from src.output.related import resolve_related
    from src.series.repository import SeriesRepository

    series_meetings = []
    if meeting.series_id and self._db.database is not None:
        series_meetings = await SeriesRepository(self._db.database).get_meetings(meeting.series_id)
    ctx.related_links = resolve_related(
        series_meetings=series_meetings,
        this_started_at=meeting.started_at or 0.0,
        project_note_name=ctx.project_name,
        vault_base=self._config.markdown.vault_path,
        client_folder=ctx.client_folder,
    )
```

- [ ] **Step 6: Commit**

```bash
git add src/output/related.py src/pipeline_runner.py tests/test_related_links.py
git commit -m "feat(output): Related links to existing series-previous and project notes"
```

---

### Task 10: Full gold-skeleton body assembly (narrative canonicalisation, overview, callouts, tables, insights, talk time)

**Files:**

- Modify: `src/output/note_assembler.py` (`split_sections`, `canonical_heading`, `render_overview`, `render_decisions`, `render_risks`, `render_action_items`, `render_talk_time`, `assemble_body` full order)
- Test: `tests/test_note_assembler.py`

**Interfaces:**

- Consumes: `NoteContext`, `render_insights_section` (existing in markdown_writer), `compute_talk_stats`.
- Produces: `split_sections(md) -> list[tuple[str, str]]`; `canonical_heading(h) -> str | None`; the section renderers; `assemble_body(ctx, transcript_link=None) -> str` emitting the gold order.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_note_assembler.py
from src.output.note_assembler import assemble_body, canonical_heading, split_sections
from src.output.note_context import ActionItemView, NoteContext


def test_split_sections_by_h2():
    md = "# Title\n\n## Summary\n\nS body\n\n## Key Decisions\n\nD body\n"
    secs = dict(split_sections(md))
    assert secs["Summary"].strip() == "S body"
    assert secs["Key Decisions"].strip() == "D body"


def test_canonical_heading_maps_and_drops():
    assert canonical_heading("Summary") == "Executive summary"
    assert canonical_heading("Key Decisions") == "Decisions made"
    assert canonical_heading("Participants") is None      # dropped, folds into overview
    assert canonical_heading("Action Items") is None      # replaced by structured table
    assert canonical_heading("Tags") is None              # moves to frontmatter
    assert canonical_heading("Executive summary") == "Executive summary"  # already-canonical passes through


def test_assemble_body_orders_gold_skeleton():
    ctx = NoteContext(
        recall_id="m1", title="Standup", date="2026-07-15", time="10:03", started_at=0.0,
        duration_minutes=28, word_count=10, enriched=True,
        attendees=["Jamie White (QVCCS)"],
        summary_markdown="# Standup\n\n## Summary\n\nWe met.\n\n## Discussion Points\n\n### Topic\n\nBody.\n\n## Notable Quotes\n\n> \"hi\" - Me\n",
        action_items=[ActionItemView(title="Do X", assignee="Amelia", status="open", due_date="2026-07-18")],
        owner_tasks=[ActionItemView(title="My thing", project_tag="project/siemens-16", priority="medium")],
        talk_stats={"speakers": [{"speaker": "Jamie White (QVCCS)", "seconds": 724.0, "turns": 34}], "total_speaking_seconds": 724.0},
        insights=[{"definition_name": "Risks", "content": "A risk"}],
        related_links=[("Previous", "2026-07-14 - Standup")],
    )
    body = assemble_body(ctx)
    order = [body.index(h) for h in ["## Related", "## Meeting overview", "## Executive summary",
                                     "## Discussion points", "## Action items", "## Insights",
                                     "## Talk time", "## My Tasks", "## Notable quotes"]]
    assert order == sorted(order)                      # sections appear in gold order
    assert "| Jamie White (QVCCS) | 12m 04s | 34 |" in body   # talk time formatted mm ss
    assert "- Previous: [[2026-07-14 - Standup]]" in body
    assert "—" not in body                        # no em dashes


def test_decisions_and_risks_render_as_callouts():
    ctx = NoteContext(recall_id="m1", title="T", date="2026-07-15", time="10:03", started_at=0.0,
                      duration_minutes=1, word_count=1, enriched=True,
                      summary_markdown="## Key Decisions\n\n- Ship it\n\n## Risks and blockers\n\n- It might break\n")
    body = assemble_body(ctx)
    assert "> [!info]" in body and "> [!warning]" in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_note_assembler.py -v`
Expected: FAIL (`split_sections` etc. not defined; body not ordered).

- [ ] **Step 3: Write the implementation**

Replace the Task 2/3 minimal `assemble_body` with the full version. Key pieces:

```python
import re

from src.output.note_context import NoteContext

_HEADING_MAP = {
    "summary": "Executive summary",
    "executive summary": "Executive summary",
    "discussion points": "Discussion points",
    "key decisions": "Decisions made",
    "decisions made": "Decisions made",
    "open questions": "Open questions",
    "open questions & risks": "Open questions",   # legacy merged heading
    "risks and blockers": "Risks and blockers",
    "notable quotes": "Notable quotes",
    "next steps": "Next steps",
}
_DROP = {"participants", "action items", "tags"}


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split on level-2 (##) headings into ordered (heading, body) pairs."""
    out: list[tuple[str, str]] = []
    current = None
    buf: list[str] = []
    for line in markdown.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m and not line.startswith("###"):
            if current is not None:
                out.append((current, "\n".join(buf).strip()))
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out.append((current, "\n".join(buf).strip()))
    return out


def canonical_heading(heading: str) -> str | None:
    key = heading.strip().lower()
    if key in _DROP:
        return None
    return _HEADING_MAP.get(key, heading.strip())


def _fmt_hms(seconds: float) -> str:
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s:02d}s"


def render_overview(ctx) -> str:
    lines = ["## Meeting overview", "", "| Field | Detail |", "|---|---|"]
    from time import localtime, strftime
    pretty = strftime("%A, %-d %B %Y", localtime(ctx.started_at)) if ctx.started_at else ctx.date
    lines.append(f"| Date | {pretty} |")
    lines.append(f"| Duration | ~{ctx.duration_minutes} minutes |")
    if ctx.attendees:
        lines.append(f"| Attendees | {', '.join(ctx.attendees)} |")
    return "\n".join(lines)


def _callout(kind: str, body: str) -> str:
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in body.splitlines())
    return f"> [!{kind}]\n{quoted}"


def render_action_items(ctx) -> str:
    if not ctx.action_items:
        return ""
    lines = ["## Action items", "", "| Action | Owner | Due | Status |", "|---|---|---|---|"]
    for it in ctx.action_items:
        due = it.due_date or "Not specified"
        owner = it.assignee or "Unassigned"
        status = it.status.replace("_", " ").capitalize()
        lines.append(f"| {it.title} | {owner} | {due} | {status} |")
    # Keep richer context as a collapsible detail (chosen during brainstorming).
    detail = [it for it in ctx.action_items if it.description]
    if detail:
        lines += ["", "> [!note]- Action item detail"]
        for it in detail:
            lines.append(f"> **{it.title}**: {it.description}")
    return "\n".join(lines)


def render_talk_time(talk_stats: dict) -> str:
    speakers = (talk_stats or {}).get("speakers") or []
    if not speakers:
        return ""
    lines = ["## Talk time", "", "| Speaker | Talk time | Turns |", "|---|---|---|"]
    for s in speakers:
        lines.append(f"| {s['speaker']} | {_fmt_hms(s.get('seconds', 0.0))} | {s.get('turns', 0)} |")
    return "\n".join(lines)


def render_related(ctx) -> str:
    if not ctx.related_links:
        return ""
    lines = ["## Related"]
    for label, name in ctx.related_links:
        lines.append(f"- {label}: [[{name}]]")
    return "\n".join(lines)
```

Then `assemble_body` composes the gold order, canonicalising narrative sections, dropping owned ones, rendering Decisions and Risks bodies as callouts, and appending footer + transcript:

```python
def assemble_body(ctx: NoteContext, transcript_link: str | None = None) -> str:
    from src.output.markdown_writer import render_insights_section

    sections = split_sections(ctx.summary_markdown)
    narrative: dict[str, str] = {}
    passthrough: list[tuple[str, str]] = []
    for heading, body in sections:
        canon = canonical_heading(heading)
        if canon is None:
            continue
        if canon in ("Executive summary", "Discussion points", "Decisions made",
                     "Open questions", "Risks and blockers", "Notable quotes", "Next steps"):
            narrative[canon] = body
        else:
            passthrough.append((canon, body))

    blocks: list[str] = [f"# {ctx.title}"]

    def add(text: str):
        if text and text.strip():
            blocks.append(text.rstrip())

    add(render_related(ctx))
    add(render_overview(ctx))
    add(_section("Executive summary", narrative.get("Executive summary")))
    add(_section("Discussion points", narrative.get("Discussion points")))
    if narrative.get("Decisions made"):
        add("## Decisions made\n\n" + _callout("info", narrative["Decisions made"]))
    add(render_action_items(ctx))
    add(_section("Open questions", narrative.get("Open questions")))
    if narrative.get("Risks and blockers"):
        add("## Risks and blockers\n\n" + _callout("warning", narrative["Risks and blockers"]))
    add(render_insights_section(ctx.insights))
    add(render_talk_time(ctx.talk_stats))
    if ctx.owner_tasks:
        add(render_my_tasks(ctx.owner_tasks))
    add(_section("Notable quotes", narrative.get("Notable quotes")))
    add(_section("Next steps", narrative.get("Next steps")))
    for heading, body in passthrough:
        add(_section(heading, body))

    footer = (
        f"---\n\n*Generated by Context Recall on {_now()}, "
        f"{ctx.duration_minutes} min, {ctx.word_count:,} words*"
    )
    blocks.append(footer)

    if ctx.transcript_mode == "linked" and transcript_link:
        add(f"## Transcript\n\n- [[{transcript_link}]]")
    else:
        tr = render_transcript(ctx.transcript, ctx.transcript_mode)
        if tr:
            blocks.append(tr)

    return "\n\n".join(b for b in blocks if b and b.strip()) + "\n"


def _section(heading: str, body: str | None) -> str:
    return f"## {heading}\n\n{body}" if body and body.strip() else ""


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M")
```

Remove the Phase-1 minimal `assemble_body` (replaced here). `render_insights_section` stays in `markdown_writer.py`; import it lazily to avoid a cycle.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_note_assembler.py -v`
Expected: PASS.

- [ ] **Step 5: Populate `ctx.insights` and `ctx.talk_stats` in `_augment_note_context`**

```python
    from src.output.markdown_writer import render_insights_section  # noqa: F401 (used in assembler)
    from src.insights.repository import InsightRepository
    from src.talk_stats import compute_talk_stats

    if self._db.database is not None:
        ctx.insights = await InsightRepository(self._db.database).results_for_meeting(meeting.id)
    ctx.talk_stats = compute_talk_stats(getattr(meeting, "transcript_json", None))
```

- [ ] **Step 6: Run the whole output suite**

Run: `python3 -m pytest tests/test_note_assembler.py tests/test_markdown_writer_write_note.py tests/test_frontmatter_parity.py tests/test_client_routing.py tests/test_my_tasks.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/output/note_assembler.py src/pipeline_runner.py tests/test_note_assembler.py
git commit -m "feat(output): assemble gold-skeleton body with callouts, tables, talk time, insights"
```

---

## Phase 6: Config surface, template alignment, hygiene, golden file

### Task 11: Template heading alignment (standard template)

**Files:**

- Modify: `src/templates.py` (`SUMMARISATION_PROMPT` headings + `standard` template `sections`)
- Test: `tests/test_templates_headings.py`

**Interfaces:**

- Produces: the `standard` template emits `Executive summary`, `Discussion points`, `Decisions made`, `Open questions`, `Risks and blockers`, `Notable quotes` (Open Questions and Risks split into two sections). `Participants` and `Action Items` headings remain in the prompt (the assembler drops/replaces them) so extraction quality is unchanged; `Tags` remains for `from_markdown`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_templates_headings.py
from src.templates import TemplateManager


def test_standard_prompt_uses_split_questions_and_risks():
    tpl = TemplateManager().get_template("standard")
    prompt = tpl.system_prompt
    assert "## Open questions" in prompt
    assert "## Risks and blockers" in prompt
    assert "## Open Questions & Risks" not in prompt
    assert "## Executive summary" in prompt
    assert "## Decisions made" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_templates_headings.py -v`
Expected: FAIL (prompt still uses old headings).

- [ ] **Step 3: Write the implementation**

Edit `SUMMARISATION_PROMPT` in `src/templates.py`: rename `## Summary` to `## Executive summary`, `## Discussion Points` to `## Discussion points`, `## Key Decisions` to `## Decisions made`, and split `## Open Questions & Risks` into a `## Open questions` bullet section and a `## Risks and blockers` bullet section (two adjacent sections with the existing per-item guidance divided appropriately). Rename `## Notable Quotes` to `## Notable quotes`. Update the `standard` template `sections` list to the new headings. Keep `## Participants`, `## Action Items`, `## Tags` in the prompt. Ensure no em dash is introduced (the existing prompt uses hyphens; keep them).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_templates_headings.py tests/test_templates*.py -v`
Expected: PASS. Update any existing template test asserting the old heading strings.

- [ ] **Step 5: Commit**

```bash
git add src/templates.py tests/test_templates_headings.py
git commit -m "feat(templates): align standard template headings with the gold note skeleton"
```

---

### Task 12: Config documentation + example taxonomy seed

**Files:**

- Modify: `config.example.yaml` (markdown block)
- Test: `tests/test_config_markdown.py`

**Interfaces:**

- Consumes: `MarkdownConfig` fields added in Task 8.
- Produces: documented `markdown` block in `config.example.yaml` including a seed `client_taxonomy` matching the vault; a test that loads `config.example.yaml` and asserts the new fields parse.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_markdown.py
from src.utils.config import load_config


def test_example_config_markdown_fields(tmp_path, monkeypatch):
    cfg = load_config("config.example.yaml")
    md = cfg.markdown
    assert md.transcript_mode in {"foldout", "linked", "omit", "inline"}
    assert md.route_by_client is True
    assert "clients" in md.client_taxonomy
    assert md.client_taxonomy["clients"]["Siemens"]["tag"] == "client/siemens"
    assert "Jamie" in md.owner_identities or "Me" in md.owner_identities
```

(If `load_config` needs a full config, extend the example file rather than mocking; `_build_dataclass` ignores unknown keys.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_markdown.py -v`
Expected: FAIL (example file lacks the new fields).

- [ ] **Step 3: Write the implementation**

Extend the `markdown:` block in `config.example.yaml`:

```yaml
markdown:
  enabled: true

  # Absolute path to the Obsidian vault meetings folder (the "70 Meetings" base).
  vault_path: "~/Documents/Meetings"

  # Filename template. Variables: {date}, {time}, {slug} (from the title).
  filename_template: "{date}_{slug}.md"

  # File each note into 70 Meetings/<Client>/ using client_taxonomy below.
  # Unknown clients fall back to Unsorted/.
  route_by_client: true

  # How the transcript is written: foldout (collapsible callout, default),
  # linked (separate companion note), omit (not written), inline (full text).
  transcript_mode: foldout

  # Legacy toggle, kept for compatibility. transcript_mode overrides it.
  include_full_transcript: false

  # Render the owner's open action items as a dashboard-wired ## My Tasks list.
  emit_my_tasks: true

  # The owner's display name in attendees and the tasks owner filter.
  owner_display_name: "Jamie White (QVCCS)"

  # Speaker labels / emails that are the owner; folded to owner_display_name.
  owner_identities:
    - Me
    - Jamie
    - jamiecs@live.co.uk
    - j65541761@gmail.com

  # Curated client/project -> vault folder and hierarchical tag. The writer
  # never invents a client/* or project/* tag; a client or project not listed
  # here is left untagged and routed to Unsorted, with a surfaced warning.
  client_taxonomy:
    clients:
      Siemens: { folder: Siemens, tag: client/siemens }
      Armacell: { folder: Armacell, tag: client/armacell }
      NTT: { folder: NTT, tag: client/ntt }
      "QVCCS Internal": { folder: QVCCS Internal, tag: qvccs-internal }
      Venetian: { folder: Unsorted, tag: client/venetian }
    projects:
      "Siemens 7 France": project/siemens-7
      "Siemens 13": project/siemens-13
      "Siemens 16 Smart UK Infrastructure": project/siemens-16
      Armacell: project/armacell
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config_markdown.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.example.yaml tests/test_config_markdown.py
git commit -m "docs(config): document enriched markdown options and seed client taxonomy"
```

---

### Task 13: Golden-file end-to-end note test

**Files:**

- Create: `tests/test_note_golden.py`, `tests/fixtures/golden_meeting_expected.md`
- Test: `tests/test_note_golden.py`

**Interfaces:**

- Consumes: everything above via `MarkdownWriter.write_note`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_note_golden.py
from pathlib import Path

from src.output.markdown_writer import MarkdownWriter
from src.output.note_context import ActionItemView, NoteContext
from src.transcriber import Transcript, TranscriptSegment
from src.utils.config import MarkdownConfig


def _ctx():
    seg = TranscriptSegment(start=0.0, end=2.0, text="Morning all.", speaker="Jamie White (QVCCS)", timestamp="00:00")
    return NoteContext(
        recall_id="4f2a", title="Morning Standup Call", date="2026-07-15", time="10:03",
        started_at=1_752_570_180.0, duration_minutes=28, word_count=4456,
        client_name="QVCCS Internal", client_folder="QVCCS Internal", client_tag="qvccs-internal",
        project_name="Siemens 16 Smart UK Infrastructure", project_tag="project/siemens-16",
        meeting_type="Standup",
        attendees=["Jamie White (QVCCS)", "Amelia Lawton (QVCCS)", "Seb (QVCCS)"],
        extra_tags=["qvccs-internal"],
        summary_markdown=(
            "# Morning Standup Call\n\n## Executive summary\n\nWe reviewed progress.\n\n"
            "## Discussion points\n\n### Callbacks\n\nDiscussed the queue.\n\n"
            "## Decisions made\n\n- Proceed with the Teams queue.\n\n"
            "## Open questions\n\n- Which methodology?\n\n"
            "## Risks and blockers\n\n- Tight timeline.\n\n"
            "## Notable quotes\n\n> \"Let us ship it.\" - Jamie\n"
        ),
        action_items=[ActionItemView(title="Rebuild callback logic", assignee="Jamie", status="open",
                                     due_date="2026-07-18", priority="medium",
                                     client_tag="", project_tag="project/siemens-16")],
        owner_tasks=[ActionItemView(title="Rebuild callback logic", project_tag="project/siemens-16",
                                    priority="medium", due_date="2026-07-18")],
        talk_stats={"speakers": [{"speaker": "Jamie White (QVCCS)", "seconds": 724.0, "turns": 34}],
                    "total_speaking_seconds": 724.0},
        insights=[], related_links=[], transcript=Transcript(segments=[seg], language="en", duration_seconds=2.0),
        transcript_mode="foldout", enriched=True,
    )


def test_golden_note_end_to_end(tmp_path):
    cfg = MarkdownConfig(enabled=True, vault_path=str(tmp_path), route_by_client=True,
                         filename_template="{date}_{slug}.md", transcript_mode="foldout")
    path = MarkdownWriter(cfg).write_note(_ctx())
    got = path.read_text(encoding="utf-8")
    # Structural assertions (not a brittle byte-for-byte match).
    assert path.parent.name == "QVCCS Internal"
    for marker in ["## Meeting overview", "## Executive summary", "## Discussion points",
                   "## Decisions made", "> [!info]", "## Action items", "## Risks and blockers",
                   "> [!warning]", "## Talk time", "## My Tasks",
                   "- [ ] Rebuild callback logic #project/siemens-16 🔼 📅 2026-07-18",
                   "> [!quote]- Full transcript"]:
        assert marker in got, marker
    assert "—" not in got  # no em dash anywhere
    import yaml
    fm = yaml.safe_load(got.split("---\n")[1])
    assert fm["enriched"] is True and isinstance(fm["attendees"], list) and isinstance(fm["tags"], list)
    assert fm["tags"] == ["qvccs-internal", "project/siemens-16"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_note_golden.py -v`
Expected: FAIL initially if any marker is missing; iterate on the assembler until all markers pass.

- [ ] **Step 3: Make it pass**

Fix any ordering or formatting gaps surfaced by the markers. No new code should be needed beyond Tasks 1-12; this test is the integration guard.

- [ ] **Step 4: Run the FULL suite + ruff**

Run: `python3 -m pytest tests/ -q && ruff check src/ tests/`
Expected: PASS (~1180+ new tests), ruff clean. Fix any regressions (notably older `tests/test_markdown_writer.py` assertions about the old footer/frontmatter shape).

- [ ] **Step 5: Commit**

```bash
git add tests/test_note_golden.py
git commit -m "test(output): golden-file end-to-end enriched note"
```

---

## Self-review (completed during planning)

**Spec coverage:**

- Phase 1 re-render + idempotency: Tasks 2, 3. ✓
- Phase 2 frontmatter parity + block lists + taxonomy tags + attendee folding: Tasks 4, 5. ✓
- Phase 3 My Tasks + dashboard query: Task 6. ✓
- Phase 4 client routing + transcript modes: Tasks 7, 8. ✓
- Phase 5 Related + callouts + talk time + insights + wikilinks: Tasks 9, 10. ✓
- Phase 6 config + template alignment + em dash + UK English: Tasks 11, 12. ✓
- Golden-file test: Task 13. ✓

**Manual-edit safety:** `_augment_note_context` reads `client_id`/`project_id` from the meeting row, which the pipeline only sets when `assignment_source != 'manual'` is not violated (manual assignments are stored on the row and thus flow through as the resolved client/project). The re-render never writes back to the meeting's client/project; it only reads. Title follows existing `preserve_title`. ✓

**Type consistency:** `NoteContext`/`ActionItemView` field names are used identically across Tasks 1, 5, 6, 8, 10, 13. `resolve_taxonomy` return type `TaxonomyResolution` consumed only in `_augment_note_context`. `assemble_body(ctx, transcript_link=None)` signature consistent between Tasks 8 and 10. ✓

**Placeholder scan:** no TBD/TODO; every code step shows code. ✓

## Notes for the implementer

- Run `git worktree` context: you are already in `.claude/worktrees/obsidian-output-enrichment` on branch `feat/obsidian-output-enrichment`.
- `Transcript.from_dict` and `TranscriptSegment` field names: confirm against `src/transcriber.py` before Task 3 and Task 13; adjust the fixture constructors if the dataclass differs.
- When editing `SUMMARISATION_PROMPT`, keep the anti-injection preamble and the "never say None" rule intact; only the section headings change.
- After Task 8 the `MarkdownConfig` gained fields; make sure `_build_dataclass` in `src/utils/config.py` maps the nested `client_taxonomy` dict through unchanged (it already ignores unknown keys and passes dicts verbatim).
- The `-%d`/`%-d` strftime directive in `render_overview` is platform-specific; on macOS `%-d` works. If a test runs on an odd platform, fall back to `str(int(...))`.
