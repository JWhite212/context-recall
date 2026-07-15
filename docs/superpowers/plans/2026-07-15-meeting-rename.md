# Meeting Rename + Calendar Auto-Title Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a meeting be renamed (post-hoc in the list/detail, and — client-side — during recording), auto-title it from the matched calendar event, and propagate a rename to the Obsidian note and Notion page — with a manual rename that survives reprocess.

**Architecture:** A meeting gains a `title_source` (`auto` | `manual`) column so the pipeline's auto-title never clobbers a user rename, and a `markdown_path` column so a rename can find and rename the written `.md` file. The pipeline sets the title from `calendar_event_title` (falling back to the summary title) unless `title_source == 'manual'`. A new `PATCH /api/meetings/{id}` sets the title as `manual`, propagates to the Obsidian note (file rename + frontmatter) and the Notion page title, and emits a `meeting.renamed` WebSocket event. Because no DB row exists during recording (it's created only when recording stops), "live rename" is client-side: the live view shows an editable title seeded from the `meeting.calendar_match` event and applies the user's pending edit via `PATCH` once the row exists (learned from `pipeline.complete`).

**Tech Stack:** Python 3.12, FastAPI, `aiosqlite`, PyYAML, `python-slugify`, `notion-client`; React 19 + TypeScript + TanStack Query + Zustand + Vitest; pytest.

## Global Constraints

- **`title_source` is `auto` | `manual` only.** `manual` is set exclusively by a user rename (the PATCH endpoint). Everything else is `auto`. Copy these two literal values verbatim.
- **Auto-title precedence:** `manual` user title > matched `calendar_event_title` > summariser `summary.title` > `"Untitled Meeting"`. The pipeline must NOT write `title`/`title_source` when the stored `title_source == 'manual'`.
- **Manual rename must survive reprocess** — the reprocess path passes `preserve_title=True` when the stored `title_source == 'manual'`.
- **No live DB row during recording.** A meeting row is created only in `src/main.py` after recording stops. "Live rename" is client-side and applied via `PATCH` once the row id is known (from `pipeline.complete`). Do NOT change the orchestrator's row-creation lifecycle in this feature.
- **Output propagation is best-effort and must never fail the rename** — the DB rename always succeeds; a markdown/Notion failure is logged and surfaced but does not 500 the request.
- **Filesystem safety:** a renamed markdown file must stay inside the vault (reuse the existing vault-escape check) and must not overwrite an unrelated existing file (collision handling).
- **macOS + Apple-Silicon only.** Python: `python3 -m pytest tests/`, `ruff check src/ tests/`. UI: `cd ui && npm test`, `npx tsc --noEmit`. Migrations bump `SCHEMA_VERSION` (currently **20 → 21**).
- Conventional-commit messages, one logical change per commit.

---

## File Structure

- **Modify** `src/db/database.py` — bump `SCHEMA_VERSION` to 21; add `title_source` + `markdown_path` to the fresh-create path and a new `if current_version < 21:` migration block; change the v20 block's `PRAGMA user_version = {SCHEMA_VERSION}` to the literal `20`.
- **Modify** `src/db/repository.py` — add both columns to `_MUTABLE_COLUMNS`, to the `MeetingRecord` dataclass, `from_row`, and `to_dict`.
- **Modify** `src/pipeline_runner.py` — persist the markdown writer's returned path (`markdown_path`); add a `preserve_title` kwarg and apply auto-title precedence at the persist step.
- **Modify** `src/api/routes/reprocess.py` — pass `preserve_title` from the stored `title_source`.
- **Modify** `src/output/notion_writer.py` — add `update_page_title(page_id, title)`.
- **Modify** `src/output/markdown_writer.py` — add `rename_note(old_path, new_title, started_at)`.
- **Create** `src/meeting_rename.py` — `apply_rename(...)`: the rename orchestration (DB + propagation + event) shared by the route.
- **Modify** `src/api/routes/meetings.py` — `PATCH /api/meetings/{meeting_id}` rename endpoint; accept an injected `event_bus`.
- **Modify** `src/api/server.py` — pass `event_bus` to `meetings_routes.init`.
- **Modify** `ui/src/lib/api.ts` — `renameMeeting(id, title)` client.
- **Modify** `ui/src/lib/types.ts` — `meeting.renamed` + `meeting.calendar_match` WS event types; `title_source`/`markdown_path` on the meeting type.
- **Modify** `ui/src/stores/appStore.ts` — handle `meeting.calendar_match` (seed live title) and `pipeline.complete` (carry `meeting_id`); expose live-title state.
- **Create** `ui/src/components/meetings/TitleEditor.tsx` — reusable inline title editor (mirrors `TagEditor.tsx`).
- **Modify** `ui/src/components/meetings/MeetingList.tsx`, `ui/src/components/meetings/MeetingDetail.tsx`, `ui/src/components/live/LiveView.tsx` — use `TitleEditor`; wire the live apply-on-complete + `meeting.renamed` invalidation.
- **Create** `ui/src/hooks/useMeetingRenamedSync.ts` — invalidate the meetings query on a `meeting.renamed` event.

---

### Task 1: Migration v21 — `title_source` + `markdown_path`

**Files:**

- Modify: `src/db/database.py:26` (`SCHEMA_VERSION`), `:607-609` (fresh-create), `:881` (v20 pragma), and add a v21 block after `:883`
- Modify: `src/db/repository.py:22-51` (`_MUTABLE_COLUMNS`), the `MeetingRecord` dataclass + `from_row` + `to_dict`
- Test: `tests/test_db_migration_v21.py` (new), `tests/test_repository.py` (existing — extend)

**Interfaces:**

- Consumes: nothing.
- Produces: `meetings.title_source` (TEXT, default `'auto'`) and `meetings.markdown_path` (TEXT, default `''`). `update_meeting(...)` accepts both; `MeetingRecord.title_source` / `.markdown_path` and their `to_dict()` keys exist.

- [ ] **Step 1: Write the failing migration test**

Create `tests/test_db_migration_v21.py` (mirror `tests/test_db_migration_v20.py`):

```python
"""v21 migration: add meetings.title_source + meetings.markdown_path."""

import pytest

from src.db.database import Database


@pytest.mark.asyncio
async def test_v21_adds_title_source_and_markdown_path(tmp_path):
    db = Database(str(tmp_path / "m.db"))
    await db.connect()
    try:
        cursor = await db.conn.execute("PRAGMA table_info(meetings)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "title_source" in cols
        assert "markdown_path" in cols

        cursor = await db.conn.execute("PRAGMA user_version")
        assert (await cursor.fetchone())[0] == 21

        mid = await db.repo.create_meeting(started_at=1.0, status="complete")
        m = await db.repo.get_meeting(mid)
        assert m.title_source == "auto"
        assert m.markdown_path == ""
    finally:
        await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration_v21.py -v`
Expected: FAIL — `title_source` not in cols / `user_version == 20`.

- [ ] **Step 3: Bump the version and the fresh-create path**

In `src/db/database.py`, change line 26:

```python
SCHEMA_VERSION = 21
```

In the fresh-create path, immediately after line 638 (`template_source`) and before line 639's trackers comment, add:

```python
            # Rename + auto-title (v21).
            await _safe_add_column(self.conn, "meetings", "title_source", "TEXT", "'auto'")
            await _safe_add_column(self.conn, "meetings", "markdown_path", "TEXT", "''")
```

- [ ] **Step 4: Un-head the v20 block and add the v21 block**

In the `if current_version < 20:` block, change line 881 from:

```python
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
```

to (so a DB at <20 doesn't skip v21):

```python
            await self.conn.execute("PRAGMA user_version = 20")
```

Then, immediately after the v20 block's `logger.info(... version 20 ...)` line (line 883), add:

```python
            current_version = 20

        if current_version < 21:
            # Rename + auto-title: title_source ('auto'|'manual') so the
            # pipeline's auto-title never clobbers a user rename, and
            # markdown_path so a rename can find + rename the .md file.
            await _safe_add_column(self.conn, "meetings", "title_source", "TEXT", "'auto'")
            await _safe_add_column(self.conn, "meetings", "markdown_path", "TEXT", "''")
            await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            await self.conn.commit()
            logger.info("Database migrated to version 21 (rename + auto-title)")
            current_version = 21
```

- [ ] **Step 5: Add the columns to the repository model + allowlist**

In `src/db/repository.py`, add to the `_MUTABLE_COLUMNS` frozenset (after `"template_source",`):

```python
        "title_source",
        "markdown_path",
```

In the `MeetingRecord` dataclass, after `notion_page_id: str = ""` (line 79), add:

```python
    title_source: str = "auto"
    markdown_path: str = ""
```

In `MeetingRecord.from_row`, mirror the existing defensive column reads (like `notion_page_id` at lines 145-147). After the `notion_page_id` read block, add:

```python
        title_source = "auto"
        markdown_path = ""
        if "title_source" in row.keys():
            title_source = row["title_source"] or "auto"
        if "markdown_path" in row.keys():
            markdown_path = row["markdown_path"] or ""
```

and pass them into the `MeetingRecord(...)` constructor (after `notion_page_id=notion_page_id,`):

```python
            title_source=title_source,
            markdown_path=markdown_path,
```

In `to_dict()`, after `"notion_page_id": self.notion_page_id,` add:

```python
            "title_source": self.title_source,
            "markdown_path": self.markdown_path,
```

- [ ] **Step 6: Add a repository test**

Add to `tests/test_repository.py`:

```python
@pytest.mark.asyncio
async def test_update_and_read_title_source_and_markdown_path(tmp_repo):
    repo = tmp_repo
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    await repo.update_meeting(mid, title_source="manual", markdown_path="/v/n.md")
    m = await repo.get_meeting(mid)
    assert m.title_source == "manual"
    assert m.markdown_path == "/v/n.md"
    assert m.to_dict()["title_source"] == "manual"
```

(Use the existing repo fixture name from that file if it differs from `tmp_repo` — read the top of `tests/test_repository.py` and match it.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_db_migration_v21.py tests/test_repository.py -v`
Expected: PASS.

- [ ] **Step 8: Guard against migration-count drift, then commit**

Run: `python3 -m pytest tests/ -q -k "migration or repository"`
Expected: PASS.

```bash
git add src/db/database.py src/db/repository.py tests/test_db_migration_v21.py tests/test_repository.py
git commit -m "feat(db): add meetings.title_source + markdown_path (migration v21)"
```

---

### Task 2: Persist `markdown_path` when the pipeline writes the note

**Files:**

- Modify: `src/pipeline_runner.py:718-758` (`_write_outputs`)
- Test: `tests/test_pipeline_runner.py` (extend — match the file's existing fixtures)

**Interfaces:**

- Consumes: `update_meeting(meeting_id, markdown_path=...)` (Task 1).
- Produces: after a run with markdown enabled, the meeting's `markdown_path` holds the written `.md` path.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pipeline_runner.py` a test that runs `_write_outputs` (or the smallest wrapper the file already uses) with a fake markdown writer whose `write(...)` returns `Path("/vault/note.md")` and `last_error = None`, and asserts the DB bridge received `update_meeting(meeting_id, markdown_path="/vault/note.md")`. Follow the file's existing pattern for constructing a `PipelineRunner` with fakes (read the top of the test file for the helper). Skeleton:

```python
def test_write_outputs_persists_markdown_path(pipeline_with_fakes):
    runner, db = pipeline_with_fakes  # db records update_meeting calls
    runner._md_writer = _FakeMd(Path("/vault/note.md"))
    runner._notion_writer = None
    runner._write_outputs(_summary(), _transcript(), 1000.0, 60.0, "m1", None)
    assert db.updates["m1"]["markdown_path"] == "/vault/note.md"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pipeline_runner.py -k markdown_path -v`
Expected: FAIL — `markdown_path` never written.

- [ ] **Step 3: Capture and persist the markdown path**

In `src/pipeline_runner.py::_write_outputs`, change the writer loop so the markdown writer's returned path is captured and persisted. Replace the loop body (lines 729-746) with:

```python
        md_path: str | None = None
        for source, writer in (
            ("markdown", self._md_writer),
            ("notion", self._notion_writer),
        ):
            if writer is None:
                continue
            try:
                result = writer.write(summary, transcript, started_at, duration_seconds)
                logger.info("%s output: %s", source.capitalize(), result)
                if source == "markdown" and result is not None:
                    md_path = str(result)
            except Exception as e:
                logger.error("%s write failed: %s", source.capitalize(), e, exc_info=True)
            if writer.last_error:
                self._emit(
                    "pipeline.warning",
                    meeting_id=meeting_id,
                    source=source,
                    message=str(writer.last_error),
                )

        if md_path and meeting_id:
            self._update(meeting_id, markdown_path=md_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_pipeline_runner.py -k markdown_path -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pipeline_runner.py tests/test_pipeline_runner.py
git commit -m "feat(pipeline): persist the written markdown path on the meeting"
```

---

### Task 3: Auto-title precedence in the pipeline (+ `preserve_title`)

**Files:**

- Modify: `src/pipeline_runner.py:208-221` (add `preserve_title` kwarg), `:341-353` (persist step)
- Modify: `src/api/routes/reprocess.py` (pass `preserve_title` from stored `title_source`)
- Test: `tests/test_pipeline_runner.py`

**Interfaces:**

- Consumes: `calendar_fields["calendar_event_title"]`, `summary.title`, the meeting's stored `title_source`.
- Produces: `_run(..., preserve_title: bool = False)`. When `preserve_title` is False, the persist step writes `title` = `calendar_event_title` (if non-empty) else `summary.title` else `"Untitled Meeting"`, and `title_source="auto"`. When True, it writes neither `title` nor `title_source`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pipeline_runner.py`:

```python
def test_auto_title_prefers_calendar_event_title(pipeline_with_fakes):
    runner, db = pipeline_with_fakes
    _run_minimal(runner, meeting_id="m1",
                 calendar_fields={"calendar_event_title": "Weekly Sync"},
                 summary_title="Discussion about the roadmap")
    assert db.updates["m1"]["title"] == "Weekly Sync"
    assert db.updates["m1"]["title_source"] == "auto"


def test_auto_title_falls_back_to_summary_title(pipeline_with_fakes):
    runner, db = pipeline_with_fakes
    _run_minimal(runner, meeting_id="m1", calendar_fields=None,
                 summary_title="Roadmap chat")
    assert db.updates["m1"]["title"] == "Roadmap chat"
    assert db.updates["m1"]["title_source"] == "auto"


def test_preserve_title_leaves_manual_title_untouched(pipeline_with_fakes):
    runner, db = pipeline_with_fakes
    _run_minimal(runner, meeting_id="m1",
                 calendar_fields={"calendar_event_title": "Weekly Sync"},
                 summary_title="Roadmap chat", preserve_title=True)
    assert "title" not in db.updates["m1"]
    assert "title_source" not in db.updates["m1"]
```

`_run_minimal` is a helper you add to the test file that drives the runner's persist step with fake transcribe/diarise/summarise (reuse the file's existing fakes; the summariser fake returns a `MeetingSummary` with the given `title`). If the file already has a full-run harness, call that with the new `preserve_title` kwarg instead.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_pipeline_runner.py -k "auto_title or preserve_title" -v`
Expected: FAIL — `title` is always `summary.title`; `preserve_title` kwarg unknown.

- [ ] **Step 3: Add the `preserve_title` kwarg**

In `src/pipeline_runner.py`, add to the `_run` signature (after `is_reprocess: bool = False,` at line 220):

```python
        preserve_title: bool = False,
```

and document it in the docstring near `is_reprocess`:

```python
            preserve_title: skip writing title/title_source — the user
                manually renamed this meeting (title_source == 'manual'),
                and a re-run must not revert it.
```

- [ ] **Step 4: Apply auto-title precedence at the persist step**

In `src/pipeline_runner.py`, replace the persist `self._update(...)` call at lines 341-353 with:

```python
        persist_fields = dict(
            ended_at=started_at + duration_seconds,
            duration_seconds=duration_seconds,
            status="complete",
            transcript_json=json.dumps(transcript.to_dict()),
            summary_markdown=summary.raw_markdown,
            tags=summary.tags,
            language=transcript.language,
            word_count=transcript.word_count,
            template_name=template.name if template else "",
            template_source=template_source,
        )
        if not preserve_title:
            calendar_title = (calendar_fields or {}).get("calendar_event_title") or ""
            persist_fields["title"] = (
                calendar_title or summary.title or "Untitled Meeting"
            )
            persist_fields["title_source"] = "auto"
        self._update(meeting_id, **persist_fields)
```

(The separate `self._update(meeting_id, **calendar_fields)` block just below at lines 354-358 stays — `calendar_event_title` is still persisted into its own column regardless.)

- [ ] **Step 5: Pass `preserve_title` from the reprocess route**

In `src/api/routes/reprocess.py`, the handler already fetches the meeting before running the pipeline. Where it calls the pipeline runner (grep for `is_reprocess=True` / the `PipelineRunner` invocation in that file), add:

```python
        preserve_title=(meeting.title_source == "manual"),
```

to that call. (Read the exact call site — the meeting variable name may be `m` or `meeting`; match it.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_pipeline_runner.py -k "auto_title or preserve_title" -v && python3 -m pytest tests/test_reprocess.py -q`
Expected: PASS (reprocess suite unaffected).

- [ ] **Step 7: Commit**

```bash
git add src/pipeline_runner.py src/api/routes/reprocess.py tests/test_pipeline_runner.py
git commit -m "feat(pipeline): calendar auto-title with manual-rename precedence"
```

---

### Task 4: `NotionWriter.update_page_title`

**Files:**

- Modify: `src/output/notion_writer.py` (add method near `archive_page`, ~line 320)
- Test: `tests/test_notion_writer.py` (extend — match its fake-client pattern)

**Interfaces:**

- Consumes: `self._config.properties["title"]`, `self._get_client()`, `self._rich_text`, `self._call_with_retry`.
- Produces: `update_page_title(self, page_id: str, title: str) -> bool` — PATCHes the page's title property; returns True on success, False on failure (never raises).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_notion_writer.py` (reuse the file's fake NotionClient — it already tests `archive_page`/`write`):

```python
def test_update_page_title_patches_title_property(notion_writer_with_fake):
    writer, fake = notion_writer_with_fake
    ok = writer.update_page_title("page-123", "Renamed Meeting")
    assert ok is True
    call = fake.pages.update_calls[-1]
    assert call["page_id"] == "page-123"
    title_prop = writer._config.properties["title"]
    assert call["properties"][title_prop]["title"][0]["text"]["content"] == "Renamed Meeting"
```

(Match the fake's recorded-calls attribute name to whatever `tests/test_notion_writer.py` already uses; read it first.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_notion_writer.py -k update_page_title -v`
Expected: FAIL — `AttributeError: 'NotionWriter' object has no attribute 'update_page_title'`.

- [ ] **Step 3: Add the method**

In `src/output/notion_writer.py`, add after `archive_page` (after line ~335):

```python
    def update_page_title(self, page_id: str, title: str) -> bool:
        """PATCH a previously written page's title property. Best-effort:
        returns False (never raises) on any Notion error so a rename's DB
        update is never blocked by an output-sync failure."""
        if not page_id:
            return False
        try:
            client = self._get_client()
            title_prop = self._config.properties["title"]
            self._call_with_retry(
                lambda: client.pages.update(
                    page_id=page_id,
                    properties={title_prop: {"title": self._rich_text(title)}},
                ),
                description="pages.update(title)",
            )
            return True
        except Exception as e:
            logger.warning("Could not update Notion page title: %s", e)
            return False
```

(Confirm `logger` is module-level in that file; it is used elsewhere in it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_notion_writer.py -k update_page_title -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/output/notion_writer.py tests/test_notion_writer.py
git commit -m "feat(notion): update_page_title for meeting rename propagation"
```

---

### Task 5: `MarkdownWriter.rename_note`

**Files:**

- Modify: `src/output/markdown_writer.py` (add method after `write`)
- Test: `tests/test_markdown_writer.py` (extend)

**Interfaces:**

- Consumes: `self._config.vault_path`, `self._config.filename_template`, `slugify`, PyYAML.
- Produces: `rename_note(self, old_path: Path, new_title: str, started_at: float) -> Path | None` — rewrites the file's frontmatter `title` to `new_title`, renames the file to the new title's slug (same template + `started_at`), returns the new `Path`. Returns the (title-updated) old path if the filename is unchanged; returns `None` on a filesystem error (sets `self.last_error`). Guards: vault-escape (raise `ValueError` like `write`) and collision (append ` (2)`, ` (3)`, … before the extension).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_markdown_writer.py`:

```python
def test_rename_note_renames_file_and_updates_frontmatter(tmp_path):
    cfg = _md_config(tmp_path)  # vault_path=tmp_path, filename_template="{date}-{slug}.md"
    w = MarkdownWriter(cfg)
    old = w.write(_summary(title="Old Title"), _transcript(), 1_000_000.0, 60.0)
    assert old is not None and old.exists()

    new = w.rename_note(old, "New Shiny Title", 1_000_000.0)
    assert new is not None and new.exists()
    assert not old.exists()
    assert "new-shiny-title" in new.name
    text = new.read_text(encoding="utf-8")
    assert "title: New Shiny Title" in text


def test_rename_note_rejects_vault_escape(tmp_path):
    w = MarkdownWriter(_md_config(tmp_path))
    old = w.write(_summary(title="X"), _transcript(), 1_000_000.0, 60.0)
    with pytest.raises(ValueError):
        w.rename_note(old, "../../etc/passwd", 1_000_000.0)


def test_rename_note_avoids_collision(tmp_path):
    w = MarkdownWriter(_md_config(tmp_path))
    a = w.write(_summary(title="Taken"), _transcript(), 1_000_000.0, 60.0)
    b = w.write(_summary(title="Other"), _transcript(), 1_000_000.0, 60.0)
    renamed = w.rename_note(b, "Taken", 1_000_000.0)
    assert renamed is not None and renamed.exists()
    assert renamed.name != a.name  # did not clobber the existing note
    assert a.exists()
```

(Reuse the file's existing `_summary`/`_transcript`/config helpers if present; otherwise add the minimal ones shown.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_markdown_writer.py -k rename_note -v`
Expected: FAIL — no `rename_note`.

- [ ] **Step 3: Add the method**

In `src/output/markdown_writer.py`, add after `write` (after line 143):

```python
    def rename_note(
        self, old_path: Path, new_title: str, started_at: float
    ) -> Path | None:
        """Rename a written note to reflect a new title.

        Rewrites the YAML frontmatter ``title`` and renames the file to the
        new title's slug (same template + start time as write()). Returns
        the new path, the (title-updated) old path when the filename is
        unchanged, or None on a filesystem error (last_error is set).
        Raises ValueError if the target would escape the vault."""
        self.last_error = None
        old_path = Path(old_path)
        vault_path = Path(self._config.vault_path)
        try:
            content = old_path.read_text(encoding="utf-8")
        except OSError as e:
            self.last_error = f"Could not read note {old_path}: {e}"
            logger.error("Rename failed: %s", self.last_error)
            return None

        # Recompute the target filename exactly like write().
        date_str = time.strftime("%Y-%m-%d", time.localtime(started_at))
        time_str = time.strftime("%H-%M", time.localtime(started_at))
        title_slug = slugify(new_title, max_length=60)
        filename = self._config.filename_template.format(
            date=date_str, time=time_str, slug=title_slug or "meeting"
        )
        filename = filename.replace("/", "_").replace("\\", "_").lstrip(".")
        new_path = (vault_path / filename).resolve()
        if not new_path.is_relative_to(vault_path.resolve()):
            raise ValueError(
                f"Rename target would escape the vault directory: {filename!r}"
            )

        # Rewrite the frontmatter title in place.
        new_content = _rewrite_frontmatter_title(content, new_title)

        # Same file: just rewrite contents.
        if new_path == old_path.resolve():
            try:
                old_path.write_text(new_content, encoding="utf-8")
            except OSError as e:
                self.last_error = f"Could not rewrite note {old_path}: {e}"
                logger.error("Rename failed: %s", self.last_error)
                return None
            return old_path

        # Different file: avoid clobbering an unrelated note.
        if new_path.exists():
            stem, suffix = new_path.stem, new_path.suffix
            n = 2
            while True:
                candidate = new_path.with_name(f"{stem} ({n}){suffix}")
                if not candidate.exists():
                    new_path = candidate
                    break
                n += 1

        try:
            new_path.write_text(new_content, encoding="utf-8")
            old_path.unlink(missing_ok=True)
        except OSError as e:
            self.last_error = f"Could not write renamed note {new_path}: {e}"
            logger.error("Rename failed: %s", self.last_error)
            return None
        logger.info("Note renamed: %s -> %s", old_path, new_path)
        return new_path
```

And add this module-level helper near the top of the file (after the imports):

```python
def _rewrite_frontmatter_title(content: str, new_title: str) -> str:
    """Return *content* with its YAML frontmatter ``title`` set to *new_title*.
    Falls back to prepending fresh frontmatter if the block is malformed."""
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end != -1:
            block = content[4:end]
            try:
                fm = _yaml.safe_load(block) or {}
            except _yaml.YAMLError:
                fm = {}
            if isinstance(fm, dict):
                fm["title"] = new_title
                new_block = _yaml.dump(
                    fm, default_flow_style=False, allow_unicode=True
                ).rstrip()
                return f"---\n{new_block}\n---{content[end + 4:]}"
    return content
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_markdown_writer.py -k rename_note -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/output/markdown_writer.py tests/test_markdown_writer.py
git commit -m "feat(markdown): rename_note for meeting rename propagation"
```

---

### Task 6: Rename orchestration + `PATCH /api/meetings/{id}`

**Files:**

- Create: `src/meeting_rename.py`
- Modify: `src/api/routes/meetings.py` (new endpoint + `event_bus` injection)
- Modify: `src/api/server.py:166` (pass `event_bus` to `meetings_routes.init`)
- Test: `tests/test_meeting_rename.py` (new), `tests/test_api_meetings.py` (extend)

**Interfaces:**

- Consumes: `repo.get_meeting`, `repo.update_meeting`, `MarkdownWriter.rename_note` (Task 5), `NotionWriter.update_page_title` (Task 4), `EventBus.emit` (`Event = dict`).
- Produces: `apply_rename(repo, meeting, new_title, *, config, event_bus, loop) -> dict` and `PATCH /api/meetings/{meeting_id}` accepting `{"title": str}` → `{"meeting_id", "title", "title_source": "manual"}`. Emits `{"type": "meeting.renamed", "meeting_id": ..., "title": ...}`.

- [ ] **Step 1: Write the failing endpoint test**

Add to `tests/test_api_meetings.py` (match its app/fixture pattern — it already tests label/tags PATCH):

```python
@pytest.mark.asyncio
async def test_patch_meeting_title_sets_manual(meetings_app):
    app, repo, bus = meetings_app  # bus records emitted events
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    await repo.update_meeting(mid, title="Auto Name", title_source="auto")

    client = TestClient(app)
    resp = client.patch(f"/api/meetings/{mid}", json={"title": "My Real Name"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "My Real Name"
    assert body["title_source"] == "manual"

    m = await repo.get_meeting(mid)
    assert m.title == "My Real Name"
    assert m.title_source == "manual"
    assert any(e["type"] == "meeting.renamed" and e["meeting_id"] == mid for e in bus.events)


@pytest.mark.asyncio
async def test_patch_meeting_title_404(meetings_app):
    app, _repo, _bus = meetings_app
    client = TestClient(app)
    assert client.patch("/api/meetings/nope", json={"title": "x"}).status_code == 404
```

If `tests/test_api_meetings.py` has no `meetings_app` fixture, add one that builds a `FastAPI()`, calls `meetings_routes.init(repo, event_bus=bus)`, includes `meetings_routes.router`, and returns `(app, repo, bus)` where `bus` is a tiny stub exposing `.emit(event)` appending to `.events` (config disables both writers so propagation is a no-op).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api_meetings.py -k patch_meeting_title -v`
Expected: FAIL — 405/404 (no `PATCH /api/meetings/{id}`).

- [ ] **Step 3: Write the rename orchestration**

Create `src/meeting_rename.py`:

```python
"""Apply a meeting rename: DB update + best-effort output propagation +
meeting.renamed event. Shared by the PATCH endpoint. Propagation failures
are logged and surfaced but never fail the rename — the DB update is the
source of truth."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("contextrecall.meeting_rename")


def _propagate(meeting, new_title: str, config) -> str | None:
    """Rename the Obsidian note + update the Notion page title (blocking).
    Runs off the event loop. Returns the new markdown path if the file was
    renamed, else None."""
    new_markdown_path: str | None = None
    if getattr(config.markdown, "enabled", False) and meeting.markdown_path:
        try:
            from src.output.markdown_writer import MarkdownWriter

            writer = MarkdownWriter(config.markdown)
            result = writer.rename_note(
                Path(meeting.markdown_path), new_title, meeting.started_at
            )
            if result is not None:
                new_markdown_path = str(result)
        except Exception as e:
            logger.warning("Markdown rename failed: %s", e)

    if getattr(config.notion, "enabled", False) and meeting.notion_page_id:
        try:
            from src.output.notion_writer import NotionWriter

            NotionWriter(config.notion).update_page_title(
                meeting.notion_page_id, new_title
            )
        except Exception as e:
            logger.warning("Notion title update failed: %s", e)

    return new_markdown_path


async def apply_rename(
    repo, meeting, new_title: str, *, config, event_bus, loop
) -> dict[str, Any]:
    """Set the title as manual, propagate to outputs, emit meeting.renamed."""
    await repo.update_meeting(meeting.id, title=new_title, title_source="manual")

    # Propagation is blocking I/O — run it off the event loop.
    new_md_path = await asyncio.get_running_loop().run_in_executor(
        None, _propagate, meeting, new_title, config
    )
    if new_md_path and new_md_path != meeting.markdown_path:
        await repo.update_meeting(meeting.id, markdown_path=new_md_path)

    if event_bus is not None:
        event_bus.emit(
            {"type": "meeting.renamed", "meeting_id": meeting.id, "title": new_title}
        )

    return {"meeting_id": meeting.id, "title": new_title, "title_source": "manual"}
```

- [ ] **Step 4: Add the endpoint + event_bus injection**

In `src/api/routes/meetings.py`, change the injection block (lines 22-28) to:

```python
# Injected at startup.
_repo = None
_event_bus = None


def init(repo, event_bus=None):
    global _repo, _event_bus
    _repo = repo
    _event_bus = event_bus
```

Add a request model near the other models (after line 40):

```python
class RenameMeetingRequest(BaseModel):
    title: str = Field(min_length=1, max_length=300)
```

Add the endpoint after `set_meeting_tags` (after line 240):

```python
@router.patch("/api/meetings/{meeting_id}", summary="Rename a meeting")
async def rename_meeting(meeting_id: str, body: RenameMeetingRequest):
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")
    from src.meeting_rename import apply_rename

    import asyncio as _asyncio

    return await apply_rename(
        _repo,
        meeting,
        body.title.strip(),
        config=load_config(),
        event_bus=_event_bus,
        loop=_asyncio.get_running_loop(),
    )
```

- [ ] **Step 5: Pass `event_bus` from the server**

In `src/api/server.py`, find the `meetings_routes.init(...)` call (grep `meetings_routes.init` or `meetings.init`) and change it to pass the bus:

```python
        meetings_routes.init(self.repo, event_bus=self.event_bus)
```

- [ ] **Step 6: Write the orchestration unit test**

Add `tests/test_meeting_rename.py`:

```python
"""apply_rename: DB update + propagation + event, all best-effort."""

import pytest

from src.meeting_rename import apply_rename


class _Bus:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)


@pytest.mark.asyncio
async def test_apply_rename_updates_db_and_emits(tmp_repo, disabled_writers_config):
    repo = tmp_repo
    mid = await repo.create_meeting(started_at=1.0, status="complete")
    await repo.update_meeting(mid, title="Auto", title_source="auto")
    meeting = await repo.get_meeting(mid)
    bus = _Bus()

    import asyncio

    out = await apply_rename(
        repo, meeting, "Manual Name",
        config=disabled_writers_config, event_bus=bus,
        loop=asyncio.get_running_loop(),
    )
    assert out == {"meeting_id": mid, "title": "Manual Name", "title_source": "manual"}
    m = await repo.get_meeting(mid)
    assert m.title == "Manual Name" and m.title_source == "manual"
    assert bus.events == [{"type": "meeting.renamed", "meeting_id": mid, "title": "Manual Name"}]
```

`disabled_writers_config` is a config object with `markdown.enabled = False` and `notion.enabled = False` (build via `load_config()` overridden, or a small dataclass stub — match how other tests in the repo build a config). Reuse `tmp_repo` from `tests/test_repository.py`'s conftest if shared, else create the DB inline.

- [ ] **Step 7: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_meeting_rename.py tests/test_api_meetings.py -v`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/meeting_rename.py src/api/routes/meetings.py src/api/server.py tests/test_meeting_rename.py tests/test_api_meetings.py
git commit -m "feat(api): PATCH /api/meetings/{id} rename with output propagation + event"
```

---

### Task 7: UI — rename client + types

**Files:**

- Modify: `ui/src/lib/api.ts` (after the meeting label/tags clients)
- Modify: `ui/src/lib/types.ts` (WS event union + meeting type)
- Test: `ui/src/lib/__tests__/api.test.ts` if present (else covered by Task 10's component tests)

**Interfaces:**

- Consumes: `PATCH /api/meetings/{id}` (Task 6).
- Produces: `renameMeeting(id: string, title: string): Promise<{ meeting_id: string; title: string; title_source: string }>`; WS event types `MeetingRenamedEvent` (`{type:"meeting.renamed"; meeting_id:string; title:string}`) and `MeetingCalendarMatchEvent` (`{type:"meeting.calendar_match"; title:string; attendees:string[]; confidence:number}`), both added to the `WSEvent` union; `title_source` + `markdown_path` added to the meeting type.

- [ ] **Step 1: Add the client**

In `ui/src/lib/api.ts`, after `setMeetingTags` (grep for the label/tags client functions), add:

```typescript
export async function renameMeeting(
  id: string,
  title: string,
): Promise<{ meeting_id: string; title: string; title_source: string }> {
  return request(`/api/meetings/${id}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}
```

(Match the exact `request(...)` signature used by `setMeetingLabel` in this file — some helpers take `{ method, body }`, confirm and mirror it.)

- [ ] **Step 2: Add the event + meeting types**

In `ui/src/lib/types.ts`, add to the WS event union (find the `type WSEvent =` / `interface` union with `pipeline.complete`, `transcript.segment`):

```typescript
export interface MeetingRenamedEvent {
  type: "meeting.renamed";
  meeting_id: string;
  title: string;
}

export interface MeetingCalendarMatchEvent {
  type: "meeting.calendar_match";
  title: string;
  attendees: string[];
  confidence: number;
}
```

and add `| MeetingRenamedEvent | MeetingCalendarMatchEvent` to the `WSEvent` union. Add `title_source?: string;` and `markdown_path?: string;` to the meeting type (the interface used for `/api/meetings` items — grep for `title:` in a `Meeting` interface). If `pipeline.complete`'s event type lacks `meeting_id`, add `meeting_id?: string;` to it (the daemon emits it — `pipeline_runner.py:369`).

- [ ] **Step 3: Type-check + commit**

Run: `cd ui && npx tsc --noEmit`
Expected: no errors.

```bash
git add ui/src/lib/api.ts ui/src/lib/types.ts
git commit -m "feat(ui): renameMeeting client + rename/calendar-match event types"
```

---

### Task 8: UI — store handling (calendar match seed + renamed invalidation)

**Files:**

- Modify: `ui/src/stores/appStore.ts` (state + `handleEvent`)
- Create: `ui/src/hooks/useMeetingRenamedSync.ts`
- Test: `ui/src/stores/__tests__/appStore.test.ts`, `ui/src/hooks/__tests__/useMeetingRenamedSync.test.tsx` (new)

**Interfaces:**

- Consumes: `MeetingCalendarMatchEvent`, `MeetingRenamedEvent`, `pipeline.complete` (with `meeting_id`) from Task 7.
- Produces: store state `liveCalendarTitle: string | null` (set from `meeting.calendar_match`, cleared on `pipeline.complete`/`resetLive`); a `useMeetingRenamedSync()` hook that invalidates `["meetings"]` + `["meeting", id]` on a `meeting.renamed` event.

- [ ] **Step 1: Write the failing store test**

Add to `ui/src/stores/__tests__/appStore.test.ts`:

```typescript
it("seeds liveCalendarTitle from meeting.calendar_match and clears on complete", () => {
  const { handleEvent } = useAppStore.getState();
  handleEvent({
    type: "meeting.calendar_match",
    title: "Weekly Sync",
    attendees: [],
    confidence: 0.9,
  });
  expect(useAppStore.getState().liveCalendarTitle).toBe("Weekly Sync");
  handleEvent({
    type: "pipeline.complete",
    meeting_id: "m1",
    title: "Weekly Sync",
  });
  expect(useAppStore.getState().liveCalendarTitle).toBeNull();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- appStore`
Expected: FAIL — `liveCalendarTitle` undefined; no case for `meeting.calendar_match`.

- [ ] **Step 3: Add the state + cases**

In `ui/src/stores/appStore.ts`, add `liveCalendarTitle: string | null` to the state interface (near `liveSegments`) and initialise it `liveCalendarTitle: null` in the store factory. In `handleEvent`, add a case:

```typescript
      case "meeting.calendar_match":
        set({ liveCalendarTitle: event.title });
        break;
```

Add `liveCalendarTitle: null` to the `set({...})` objects in both the `pipeline.complete` case (line 131) and `resetLive` (line 184). (A `meeting.renamed` case is NOT added here — query invalidation lives in the hook below, which has `queryClient` access the store lacks.)

- [ ] **Step 4: Add the renamed-sync hook**

Create `ui/src/hooks/useMeetingRenamedSync.ts`:

```typescript
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { WSEvent } from "../lib/types";

/**
 * Invalidate cached meeting data when any client renames a meeting, so the
 * list and detail views reflect the new title. Pass the same WS event
 * stream the store consumes (subscribe wherever handleEvent is dispatched).
 */
export function useMeetingRenamedSync(lastEvent: WSEvent | null): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!lastEvent || lastEvent.type !== "meeting.renamed") return;
    queryClient.invalidateQueries({ queryKey: ["meetings"] });
    queryClient.invalidateQueries({
      queryKey: ["meeting", lastEvent.meeting_id],
    });
  }, [lastEvent, queryClient]);
}
```

(Read the component that dispatches `handleEvent` on each WS message — grep for `handleEvent(`. If it keeps the latest event in a ref/state, pass it to `useMeetingRenamedSync`; if not, add a `lastEvent` state there and set it alongside the `handleEvent` call. Match the actual dispatch site.)

- [ ] **Step 5: Write the hook test**

Create `ui/src/hooks/__tests__/useMeetingRenamedSync.test.tsx`:

```typescript
import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useMeetingRenamedSync } from "../useMeetingRenamedSync";

describe("useMeetingRenamedSync", () => {
  it("invalidates meeting queries on a rename event", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    renderHook(
      () =>
        useMeetingRenamedSync({ type: "meeting.renamed", meeting_id: "m1", title: "New" }),
      { wrapper },
    );
    expect(spy).toHaveBeenCalledWith({ queryKey: ["meetings"] });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["meeting", "m1"] });
  });
});
```

- [ ] **Step 6: Run tests + type-check**

Run: `cd ui && npm test -- appStore useMeetingRenamedSync && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 7: Commit**

```bash
git add ui/src/stores/appStore.ts ui/src/hooks/useMeetingRenamedSync.ts ui/src/stores/__tests__/appStore.test.ts ui/src/hooks/__tests__/useMeetingRenamedSync.test.tsx
git commit -m "feat(ui): seed live title from calendar match + invalidate on rename"
```

---

### Task 9: UI — reusable `TitleEditor`

**Files:**

- Create: `ui/src/components/meetings/TitleEditor.tsx`
- Test: `ui/src/components/meetings/__tests__/TitleEditor.test.tsx` (new)

**Interfaces:**

- Consumes: `renameMeeting` (Task 7).
- Produces: `<TitleEditor meetingId={string} title={string} onRenamed?={(t: string) => void} className?={string} />` — an inline click-to-edit title. Enter/blur commits via `renameMeeting` + a `["meetings"]`/`["meeting", id]` invalidation and calls `onRenamed`; Escape cancels; empty/whitespace is rejected (keeps the old title).

- [ ] **Step 1: Write the failing test**

Create `ui/src/components/meetings/__tests__/TitleEditor.test.tsx`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { TitleEditor } from "../TitleEditor";
import { makeWrapper } from "../../../test/queryWrapper";

describe("TitleEditor", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn(async () =>
      new Response(
        JSON.stringify({ meeting_id: "m1", title: "Renamed", title_source: "manual" }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
  });

  it("commits a new title on Enter", async () => {
    const onRenamed = vi.fn();
    render(<TitleEditor meetingId="m1" title="Old" onRenamed={onRenamed} />, {
      wrapper: makeWrapper(),
    });
    fireEvent.click(screen.getByText("Old"));
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onRenamed).toHaveBeenCalledWith("Renamed"));
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/meetings/m1"),
      expect.objectContaining({ method: "PATCH" }),
    );
  });

  it("cancels on Escape without calling the API", () => {
    render(<TitleEditor meetingId="m1" title="Old" />, { wrapper: makeWrapper() });
    fireEvent.click(screen.getByText("Old"));
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "Nope" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.getByText("Old")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ui && npm test -- TitleEditor`
Expected: FAIL — no `TitleEditor`.

- [ ] **Step 3: Implement the component**

Create `ui/src/components/meetings/TitleEditor.tsx`:

```tsx
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { renameMeeting } from "../../lib/api";
import { useToast } from "../common/Toast";

export function TitleEditor({
  meetingId,
  title,
  onRenamed,
  className,
}: {
  meetingId: string;
  title: string;
  onRenamed?: (title: string) => void;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(title);

  const rename = useMutation({
    mutationFn: (next: string) => renameMeeting(meetingId, next),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["meetings"] });
      queryClient.invalidateQueries({ queryKey: ["meeting", meetingId] });
      onRenamed?.(data.title);
      setEditing(false);
    },
    onError: () => {
      toast.error("Failed to rename meeting.");
      setEditing(false);
    },
  });

  function commit() {
    const next = value.trim();
    if (!next || next === title) {
      setValue(title);
      setEditing(false);
      return;
    }
    rename.mutate(next);
  }

  if (!editing) {
    return (
      <button
        type="button"
        title="Click to rename"
        onClick={() => {
          setValue(title);
          setEditing(true);
        }}
        className={className ?? "text-left"}
      >
        {title}
      </button>
    );
  }

  return (
    <input
      autoFocus
      value={value}
      disabled={rename.isPending}
      onChange={(e) => setValue(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setValue(title);
          setEditing(false);
        }
      }}
      className={className ?? "bg-surface border border-border rounded px-1"}
    />
  );
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ui && npm test -- TitleEditor && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/meetings/TitleEditor.tsx ui/src/components/meetings/__tests__/TitleEditor.test.tsx
git commit -m "feat(ui): reusable inline TitleEditor"
```

---

### Task 10: UI — wire rename into list, detail, and live view

**Files:**

- Modify: `ui/src/components/meetings/MeetingList.tsx`, `ui/src/components/meetings/MeetingDetail.tsx`, `ui/src/components/live/LiveView.tsx`
- Test: extend each component's test under the respective `__tests__/`

**Interfaces:**

- Consumes: `TitleEditor` (Task 9), `renameMeeting` (Task 7), `liveCalendarTitle` + `pipeline.complete` `meeting_id` (Task 8).
- Produces: post-hoc rename in the list row and detail header; a live editable title in `LiveView` that applies the pending edit via `renameMeeting` when `pipeline.complete` arrives with a `meeting_id`.

- [ ] **Step 1: Post-hoc rename — MeetingDetail header**

Read `ui/src/components/meetings/MeetingDetail.tsx`, find where the meeting title is rendered in the header (grep `meeting.title` / an `<h1>`/`<h2>`). Replace that title node with:

```tsx
<TitleEditor
  meetingId={meeting.id}
  title={meeting.title}
  className="text-2xl font-semibold text-text-primary text-left"
/>
```

(Import `TitleEditor` from `./TitleEditor`; keep the surrounding layout classes — move them onto the `className` prop so the visual weight is unchanged.) Add/extend the detail test to click the title, type a new value, press Enter, and assert `fetch` was called with `PATCH /api/meetings/{id}`.

- [ ] **Step 2: Post-hoc rename — MeetingList row**

Read `ui/src/components/meetings/MeetingList.tsx`, find the row's title text. Wrap it with `TitleEditor` the same way, but guard against the row's click-through-to-detail: render the editor in its own element and `stopPropagation` on its container so entering edit mode doesn't navigate. Extend the list test to assert an inline edit issues the PATCH and does not navigate.

- [ ] **Step 3: Write the failing live-view test**

In `ui/src/components/live/__tests__/LiveView.test.tsx` (create if absent), add a test: render `LiveView`; dispatch a `meeting.calendar_match` event (via the store's `handleEvent`) with `title: "Weekly Sync"`; assert the live title shows "Weekly Sync"; edit it to "My Notes"; dispatch `pipeline.complete` with `{ meeting_id: "m9" }`; assert `fetch` was called with `PATCH /api/meetings/m9` and body `{"title":"My Notes"}`.

- [ ] **Step 4: Implement the live editable title + apply-on-complete**

In `ui/src/components/live/LiveView.tsx`:

- Read `liveCalendarTitle` from the store (`useAppStore((s) => s.liveCalendarTitle)`).
- Keep local state `const [liveTitle, setLiveTitle] = useState<string | null>(null)` and `const editedRef = useRef(false)`. When `liveCalendarTitle` changes and the user hasn't edited, sync `liveTitle` to it.
- Render an `<input>` bound to `liveTitle ?? liveCalendarTitle ?? ""`; on change set `liveTitle` and `editedRef.current = true`.
- Subscribe to the terminal event: when a `pipeline.complete` event with a `meeting_id` is observed (thread it through the same last-event mechanism used by `useMeetingRenamedSync`, or read a store field set in the `pipeline.complete` case), if `editedRef.current` and the local title differs from the calendar title, call `renameMeeting(meeting_id, liveTitle)`. Reset `editedRef` and `liveTitle` afterwards.

Provide the exact code matching `LiveView.tsx`'s existing structure (hooks at top, JSX title slot). Keep the transcript/audio-level UI untouched.

- [ ] **Step 5: Run the UI tests + type-check**

Run: `cd ui && npm test -- MeetingDetail MeetingList LiveView && npx tsc --noEmit`
Expected: PASS, no type errors.

- [ ] **Step 6: Commit**

```bash
git add ui/src/components/meetings/MeetingList.tsx ui/src/components/meetings/MeetingDetail.tsx ui/src/components/live/LiveView.tsx ui/src/components/meetings/__tests__ ui/src/components/live/__tests__
git commit -m "feat(ui): inline rename in list/detail + live editable title"
```

---

### Task 11: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Python suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass (existing count + the new rename/migration/writer tests). If a pre-existing test constructs a meeting and asserts an exact `to_dict()` shape, update it to include the new `title_source`/`markdown_path` keys.

- [ ] **Step 2: Python lint**

Run: `ruff check src/ tests/`
Expected: clean.

- [ ] **Step 3: UI tests + type-check**

Run: `cd ui && npm test && npx tsc --noEmit`
Expected: clean.

- [ ] **Step 4: Commit any verification fixes**

```bash
git add -A
git commit -m "test(rename): fix assertions surfaced by rename + auto-title"
```

---

## Manual verification (not automatable in CI)

1. Record a meeting that matches a calendar event → the meeting's title is the calendar event's title (`title_source == auto`); the live view shows that title during recording.
2. Rename it in the meetings list; reopen — the new name persists; the Obsidian note file is renamed and its frontmatter `title` updated; the Notion page title updates.
3. Reprocess the renamed meeting → the manual name is **not** reverted to the calendar/summary title.
4. During a recording, edit the live title and let it finish → the finished meeting carries your edited name.

---

## Self-Review

- **Spec coverage:** `PATCH /api/meetings/{id}` → Task 6; `title_source` (auto|manual) → Tasks 1/3/6; auto-title from `calendar_event_title` → Task 3; live rename (client-side, applied on `pipeline.complete`) → Tasks 8/10 (matches the brainstorming decision that no live DB row exists); post-hoc inline rename in list + detail → Task 10; propagation to Obsidian file+frontmatter and Notion title with collision + vault-escape guards → Tasks 4/5/6; manual survives reprocess → Task 3 (`preserve_title`). `meeting.renamed` event → Tasks 6/7/8.
- **Placeholder scan:** none — each code/test step shows the code; where a host file's internals must be matched (MeetingList/MeetingDetail/LiveView title slot, the WS dispatch site, config/repo fixture names), the task names the exact grep to locate it and the exact change to make, because those spots vary and inventing their surrounding code would be wrong.
- **Type consistency:** `title_source`/`markdown_path` are introduced in Task 1 and used identically in Tasks 3/6; `renameMeeting` (Task 7) returns `{meeting_id,title,title_source}` and is consumed unchanged in Tasks 9/10; `meeting.renamed` shape `{type,meeting_id,title}` is emitted in Task 6 and typed/consumed in Tasks 7/8; `rename_note(old_path,new_title,started_at)` and `update_page_title(page_id,title)` signatures match between their definition (Tasks 5/4) and caller (Task 6).
