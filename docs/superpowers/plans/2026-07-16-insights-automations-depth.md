# Insights + Automations Depth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring Context Recall's existing Insights and Automations features to Circleback parity: structured (typed) insight fields, automation actions that run insights and POST real meeting data, and a seeded starter set.

**Architecture:** Purely additive on top of the shipped `src/insights/`, `src/automations/`, and their UI. One DB migration (v23) adds two columns to `insight_definitions`, one to `insight_results`, and an `app_metadata` KV table. New automation action _types_ need no migration (actions are stored as JSON). Backward compatibility is guaranteed by `output_mode` defaulting to `'list'` (today's behaviour).

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, pydantic; React 19 + TypeScript + Vitest; pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-16-insights-automations-depth-design.md`

## Global Constraints

- macOS + Apple Silicon; local-first daemon on `127.0.0.1:9876`.
- `SCHEMA_VERSION` head becomes **23**. Migrations are additive; use `_safe_add_column`.
- LLM calls go through `Summariser` (dual backend: `claude` / `ollama`). Never call a model directly.
- Post-processing steps are **guarded / non-fatal** — a failure logs a warning and never breaks the pipeline.
- Blocking LLM/HTTP calls run off the event loop via `asyncio.to_thread`.
- Reprocess-safe: content-producing steps use delete-then-insert; side-effect actions gate on `run_side_effects`.
- TDD: failing test first, minimal impl, green, commit. Run `ruff check src/ tests/` before each backend commit and `npx tsc --noEmit` before each UI commit.
- Field type vocabulary is exactly: `text`, `number`, `date`, `boolean`, `list`.

---

## File Structure

**Create:**

- `src/automations/payload.py` — pure Circleback-schema payload builder + HMAC signer.
- `src/insights/seed.py` — idempotent starter-content seeding.
- `tests/test_insights_structured.py`, `tests/test_insights_repository_structured.py`,
  `tests/test_db_migration_v23.py`, `tests/test_automation_payload.py`,
  `tests/test_automation_actions.py`, `tests/test_insights_seed.py`,
  `tests/test_insights_route_structured.py`.
- UI tests under existing `__tests__` dirs.

**Modify:**

- `src/db/database.py` — `SCHEMA_VERSION`, `APP_METADATA_SQL`, v23 migration (both paths), `INSIGHT_DEFINITIONS_SQL`/`INSIGHT_RESULTS_SQL` unchanged (columns added via `_safe_add_column`).
- `src/db/repository.py` — `get_meta`/`set_meta` helpers on `MeetingRepository` (or `Database`).
- `src/insights/repository.py` — structured fields, `replace_results_for_definition`.
- `src/insights/extractor.py` — structured extraction + coercion + rendering.
- `src/api/routes/insights.py` — validate/accept `output_mode` + `fields`.
- `src/transcriber.py` — `Transcript.from_dict`.
- `src/automations/executor.py` — `run_insight` + `send_notes` actions, services bundle.
- `src/pipeline_runner.py` — build services bundle in `_run_automations`; call seed at boot (via server).
- `src/api/server.py` — run `seed_starter_content` once after repos init.
- `src/output/markdown_writer.py`, `src/output/notion_writer.py` — render insights (Task 12).
- UI: `ui/src/lib/types.ts`, `ui/src/lib/api.ts`, `ui/src/components/settings/InsightsSection.tsx`,
  `ui/src/components/meetings/MeetingInsights.tsx`, `ui/src/components/settings/AutomationsSection.tsx`.

---

## Task 1: Migration v23 — structured columns + app_metadata

**Files:**

- Modify: `src/db/database.py`
- Modify: `src/db/repository.py` (add `get_meta`/`set_meta`)
- Test: `tests/test_db_migration_v23.py`

**Interfaces:**

- Produces: `SCHEMA_VERSION == 23`; `insight_definitions.output_mode` (default `'list'`), `insight_definitions.fields_json` (NULL), `insight_results.fields_json` (NULL); table `app_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)`.
- Produces: `MeetingRepository.get_meta(key) -> str | None`, `MeetingRepository.set_meta(key, value) -> None`.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/test_db_migration_v23.py
import json
from src.db.database import SCHEMA_VERSION, Database


async def test_v22_db_migrates_to_v23_with_new_columns(tmp_path):
    db_path = tmp_path / "v22.db"
    db = Database(db_path=db_path)
    await db.connect()
    # Insert a pre-v23 insight definition, then rewind to 22.
    await db.conn.execute(
        "INSERT INTO insight_definitions (id, name, prompt, enabled, created_at, updated_at) "
        "VALUES ('d1', 'Questions', 'List questions', 1, 1.0, 1.0)"
    )
    await db.conn.execute("PRAGMA user_version = 22")
    await db.conn.commit()
    await db.close()

    db2 = Database(db_path=db_path)
    await db2.connect()
    try:
        cur = await db2.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION == 23
        # Old row defaults to list mode, null fields.
        cur = await db2.conn.execute(
            "SELECT output_mode, fields_json FROM insight_definitions WHERE id = 'd1'"
        )
        row = await cur.fetchone()
        assert row["output_mode"] == "list"
        assert row["fields_json"] is None
        # app_metadata usable.
        await db2.conn.execute("INSERT INTO app_metadata (key, value) VALUES ('k', 'v')")
        await db2.conn.commit()
        cur = await db2.conn.execute("SELECT value FROM app_metadata WHERE key = 'k'")
        assert (await cur.fetchone())["value"] == "v"
    finally:
        await db2.close()


async def test_get_set_meta_roundtrip(tmp_path):
    from src.db.repository import MeetingRepository
    db = Database(db_path=tmp_path / "meta.db")
    await db.connect()
    try:
        repo = MeetingRepository(db)
        assert await repo.get_meta("missing") is None
        await repo.set_meta("insights_seed_version", "1")
        assert await repo.get_meta("insights_seed_version") == "1"
        await repo.set_meta("insights_seed_version", "2")  # upsert
        assert await repo.get_meta("insights_seed_version") == "2"
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_db_migration_v23.py -v`
Expected: FAIL (SCHEMA_VERSION == 22; `output_mode` column missing; `get_meta` missing).

- [ ] **Step 3: Add the `app_metadata` DDL constant**

In `src/db/database.py`, near the other `*_SQL` constants (e.g. after `AUTOMATION_DISPATCHES_SQL`):

```python
APP_METADATA_SQL = """
CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""
```

- [ ] **Step 4: Bump the version and extend both migration paths**

Change `SCHEMA_VERSION = 22` → `SCHEMA_VERSION = 23`.

In the fresh-create block (`if current_version < 1:`), immediately **before** `await self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")` (~line 672), add:

```python
            # Structured insights + app metadata (v23).
            await _safe_add_column(self.conn, "insight_definitions", "output_mode", "TEXT", "'list'")
            await _safe_add_column(self.conn, "insight_definitions", "fields_json", "TEXT", "NULL")
            await _safe_add_column(self.conn, "insight_results", "fields_json", "TEXT", "NULL")
            await self.conn.executescript(APP_METADATA_SQL)
```

Then add a new incremental block after the `if current_version < 22:` block:

```python
        if current_version < 23:
            # Structured (typed) insights + a generic key/value store.
            await _safe_add_column(self.conn, "insight_definitions", "output_mode", "TEXT", "'list'")
            await _safe_add_column(self.conn, "insight_definitions", "fields_json", "TEXT", "NULL")
            await _safe_add_column(self.conn, "insight_results", "fields_json", "TEXT", "NULL")
            await self.conn.executescript(APP_METADATA_SQL)
            await self.conn.execute("PRAGMA user_version = 23")
            await self.conn.commit()
            logger.info("Database migrated to version 23 (structured insights + app_metadata)")
            current_version = 23
```

- [ ] **Step 5: Add `get_meta`/`set_meta` to `MeetingRepository`**

In `src/db/repository.py`, add methods on `MeetingRepository`:

```python
    async def get_meta(self, key: str) -> str | None:
        cur = await self._db.conn.execute("SELECT value FROM app_metadata WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO app_metadata (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            await self._db.conn.commit()
```

(Use the same `self._db` attribute name the class already uses; confirm by reading the top of `MeetingRepository`.)

- [ ] **Step 6: Run tests to verify pass**

Run: `python3 -m pytest tests/test_db_migration_v23.py -v && ruff check src/`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/db/database.py src/db/repository.py tests/test_db_migration_v23.py
git commit -m "feat(db): v23 — structured-insight columns + app_metadata KV store"
```

---

## Task 2: InsightRepository — structured fields + scoped result replace

**Files:**

- Modify: `src/insights/repository.py`
- Test: `tests/test_insights_repository_structured.py`

**Interfaces:**

- Consumes: v23 columns (Task 1).
- Produces:
  - `create(name, prompt, enabled=True, output_mode="list", fields=None) -> str`
  - `update(insight_id, *, name=None, prompt=None, enabled=None, output_mode=None, fields=None)`
  - `_row_to_dict` now returns `output_mode: str` and `fields: list[dict] | None`.
  - `replace_results_for_definition(meeting_id, definition_id, results) -> int`
  - result rows accept optional `fields: dict | None`; `results_for_meeting` returns `fields`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_insights_repository_structured.py
from src.db.database import Database
from src.insights.repository import InsightRepository


async def _repo(tmp_path):
    db = Database(db_path=tmp_path / "ins.db")
    await db.connect()
    return db, InsightRepository(db)


async def test_create_structured_definition_roundtrips_fields(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        fields = [{"key": "go_live_date", "label": "Go-live date", "type": "date"},
                  {"key": "blockers", "label": "Blockers", "type": "list"}]
        did = await repo.create("Client Call", "Extract client details",
                                output_mode="structured", fields=fields)
        got = await repo.get(did)
        assert got["output_mode"] == "structured"
        assert got["fields"] == fields
    finally:
        await db.close()


async def test_list_definition_defaults_to_list_mode(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        did = await repo.create("Questions", "List questions asked")
        got = await repo.get(did)
        assert got["output_mode"] == "list"
        assert got["fields"] is None
    finally:
        await db.close()


async def test_replace_results_for_definition_isolates_definitions(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        from src.db.repository import MeetingRepository
        mrepo = MeetingRepository(db)
        mid = await mrepo.create_meeting(started_at=1.0)
        # Global write of two definitions' results.
        await repo.replace_results_for_meeting(mid, [
            {"definition_id": "A", "definition_name": "A", "content": "a1", "speaker": ""},
            {"definition_id": "B", "definition_name": "B", "content": "b1", "speaker": ""},
        ])
        # Scoped re-run of B only must not touch A.
        n = await repo.replace_results_for_definition(mid, "B", [
            {"definition_id": "B", "definition_name": "B", "content": "b2",
             "speaker": "", "fields": {"x": 1}},
        ])
        assert n == 1
        rows = await repo.results_for_meeting(mid)
        contents = {(r["definition_id"], r["content"]) for r in rows}
        assert ("A", "a1") in contents
        assert ("B", "b2") in contents
        assert ("B", "b1") not in contents
        b_row = next(r for r in rows if r["definition_id"] == "B")
        assert b_row["fields"] == {"x": 1}
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_insights_repository_structured.py -v`
Expected: FAIL (`create` has no `output_mode`; no `replace_results_for_definition`).

- [ ] **Step 3: Implement structured support**

In `src/insights/repository.py`, add `import json` at top if absent. Update:

```python
    async def create(self, name, prompt, enabled=True, output_mode="list", fields=None) -> str:
        insight_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO insight_definitions "
                "(id, name, prompt, enabled, output_mode, fields_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (insight_id, name, prompt, 1 if enabled else 0, output_mode,
                 json.dumps(fields) if fields else None, now, now),
            )
            await self._db.conn.commit()
        return insight_id

    async def update(self, insight_id, *, name=None, prompt=None, enabled=None,
                     output_mode=None, fields=None) -> None:
        sets, vals = [], []
        if name is not None:
            sets.append("name = ?"); vals.append(name)
        if prompt is not None:
            sets.append("prompt = ?"); vals.append(prompt)
        if enabled is not None:
            sets.append("enabled = ?"); vals.append(1 if enabled else 0)
        if output_mode is not None:
            sets.append("output_mode = ?"); vals.append(output_mode)
        if fields is not None:
            sets.append("fields_json = ?"); vals.append(json.dumps(fields) if fields else None)
        if not sets:
            return
        sets.append("updated_at = ?"); vals.append(time.time())
        vals.append(insight_id)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE insight_definitions SET {', '.join(sets)} WHERE id = ?", vals
            )
            await self._db.conn.commit()
```

Update `_row_to_dict`:

```python
    @staticmethod
    def _row_to_dict(row) -> dict:
        raw_fields = row["fields_json"] if "fields_json" in row.keys() else None
        try:
            fields = json.loads(raw_fields) if raw_fields else None
        except (ValueError, TypeError):
            fields = None
        return {
            "id": row["id"],
            "name": row["name"],
            "prompt": row["prompt"],
            "enabled": bool(row["enabled"]),
            "output_mode": row["output_mode"] if "output_mode" in row.keys() else "list",
            "fields": fields,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
```

Update `replace_results_for_meeting` INSERT to also write `fields_json`, and `results_for_meeting` SELECT to return it. Add the scoped variant. The INSERT column list becomes `(definition_id, definition_name, meeting_id, content, speaker, fields_json, created_at)`:

```python
    async def _insert_results(self, meeting_id, results, now):
        for r in results:
            f = r.get("fields")
            await self._db.conn.execute(
                "INSERT INTO insight_results "
                "(definition_id, definition_name, meeting_id, content, speaker, fields_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["definition_id"], r["definition_name"], meeting_id, r["content"],
                 r.get("speaker", ""), json.dumps(f) if f is not None else None, now),
            )

    async def replace_results_for_meeting(self, meeting_id: str, results: list[dict]) -> int:
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "DELETE FROM insight_results WHERE meeting_id = ?", (meeting_id,)
            )
            await self._insert_results(meeting_id, results, now)
            await self._db.conn.commit()
        return len(results)

    async def replace_results_for_definition(
        self, meeting_id: str, definition_id: str, results: list[dict]
    ) -> int:
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "DELETE FROM insight_results WHERE meeting_id = ? AND definition_id = ?",
                (meeting_id, definition_id),
            )
            await self._insert_results(meeting_id, results, now)
            await self._db.conn.commit()
        return len(results)

    async def results_for_meeting(self, meeting_id: str) -> list[dict]:
        cursor = await self._db.conn.execute(
            "SELECT definition_id, definition_name, content, speaker, fields_json "
            "FROM insight_results WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        )
        out = []
        for r in await cursor.fetchall():
            raw = r["fields_json"]
            try:
                fields = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                fields = None
            out.append({
                "definition_id": r["definition_id"],
                "definition_name": r["definition_name"],
                "content": r["content"],
                "speaker": r["speaker"],
                "fields": fields,
            })
        return out
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_insights_repository_structured.py -v && ruff check src/insights/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/insights/repository.py tests/test_insights_repository_structured.py
git commit -m "feat(insights): structured fields + scoped per-definition result replace"
```

---

## Task 3: InsightExtractor — structured extraction, coercion, rendering

**Files:**

- Modify: `src/insights/extractor.py`
- Test: `tests/test_insights_structured.py`

**Interfaces:**

- Consumes: definitions carrying `output_mode` + `fields` (Task 2).
- Produces: `extract()` items gain optional `fields: dict | None`. For a structured definition, exactly one item is produced with `fields` = coerced record and `content` = human rendering. Pure helpers: `coerce_value(value, type_) -> Any`, `render_content(fields, field_defs) -> str`, `parse_structured(response, definition) -> list[dict]`.

- [ ] **Step 1: Write failing tests (pure helpers, no LLM)**

```python
# tests/test_insights_structured.py
from src.insights.extractor import InsightExtractor, coerce_value, render_content


def test_coerce_number_date_boolean_list():
    assert coerce_value("42", "number") == 42
    assert coerce_value(3.5, "number") == 3.5
    assert coerce_value("not a num", "number") is None
    assert coerce_value("2026-09-02", "date") == "2026-09-02"
    assert coerce_value("02/09/2026", "date") is None  # only ISO accepted
    assert coerce_value("yes", "boolean") is True
    assert coerce_value(False, "boolean") is False
    assert coerce_value("Findings", "list") == ["Findings"]
    assert coerce_value(["a", "b"], "list") == ["a", "b"]
    assert coerce_value("plain", "text") == "plain"
    assert coerce_value(None, "text") is None


def test_render_content_joins_labels():
    fields = [{"key": "go_live", "label": "Go-live", "type": "date"},
              {"key": "blockers", "label": "Blockers", "type": "list"}]
    record = {"go_live": "2026-09-02", "blockers": ["A", "B"]}
    out = render_content(record, fields)
    assert "Go-live: 2026-09-02" in out
    assert "Blockers: A; B" in out


def test_parse_structured_coerces_and_builds_one_item():
    fields = [{"key": "count", "label": "Count", "type": "number"},
              {"key": "items", "label": "Items", "type": "list"}]
    definition = {"id": "d", "name": "Snapshot", "output_mode": "structured", "fields": fields}
    ext = InsightExtractor.__new__(InsightExtractor)  # no LLM init needed for parse
    out = ext.parse_structured('{"count": "5", "items": ["x", "y"]}', definition)
    assert len(out) == 1
    assert out[0]["fields"] == {"count": 5, "items": ["x", "y"]}
    assert out[0]["definition_id"] == "d"
    assert "Count: 5" in out[0]["content"]


def test_parse_structured_missing_field_is_null():
    fields = [{"key": "owner", "label": "Owner", "type": "text"}]
    definition = {"id": "d", "name": "X", "output_mode": "structured", "fields": fields}
    ext = InsightExtractor.__new__(InsightExtractor)
    out = ext.parse_structured("{}", definition)
    assert out[0]["fields"] == {"owner": None}


def test_parse_structured_malformed_json_returns_empty():
    definition = {"id": "d", "name": "X", "output_mode": "structured",
                  "fields": [{"key": "a", "label": "A", "type": "text"}]}
    ext = InsightExtractor.__new__(InsightExtractor)
    assert ext.parse_structured("not json", definition) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_insights_structured.py -v`
Expected: FAIL (`coerce_value`, `render_content`, `parse_structured` undefined).

- [ ] **Step 3: Implement coercion, rendering, structured prompt + parse, and route in `extract`**

In `src/insights/extractor.py` add module-level helpers and a structured system prompt:

```python
_STRUCTURED_SYSTEM_PROMPT = """You extract a structured record from a meeting transcript.

The user wants: {instruction}

Return ONLY a single JSON object with EXACTLY these keys:
{field_lines}

Rules:
- date fields: ISO format "YYYY-MM-DD" or null if not stated.
- number fields: a JSON number or null.
- boolean fields: true or false.
- list fields: a JSON array of short strings (empty array if none).
- text fields: a short string or null.
Use null when the transcript does not state a value. No explanation, no markdown."""


def coerce_value(value, type_):
    if value is None:
        return None
    if type_ == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            f = float(str(value).strip())
            return int(f) if f.is_integer() else f
        except (ValueError, TypeError):
            return None
    if type_ == "date":
        import re as _re
        s = str(value).strip()
        return s if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else None
    if type_ == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "yes", "y", "1"}
    if type_ == "list":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        s = str(value).strip()
        return [s] if s else []
    # text
    s = str(value).strip()
    return s or None


def render_content(record, field_defs):
    parts = []
    for f in field_defs:
        v = record.get(f["key"])
        if v is None or v == [] or v == "":
            continue
        shown = "; ".join(str(x) for x in v) if isinstance(v, list) else str(v)
        parts.append(f"{f['label']}: {shown}")
    return " · ".join(parts)
```

Add `parse_structured` and wire `extract` to branch on `output_mode`:

````python
    def parse_structured(self, response: str, definition: dict) -> list[dict]:
        if not response:
            return []
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", response.strip())
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if not match:
                return []
            try:
                obj = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        if not isinstance(obj, dict):
            return []
        field_defs = definition.get("fields") or []
        record = {f["key"]: coerce_value(obj.get(f["key"]), f["type"]) for f in field_defs}
        return [{
            "definition_id": definition["id"],
            "definition_name": definition["name"],
            "content": render_content(record, field_defs),
            "speaker": "",
            "fields": record,
        }]
````

In `extract`, branch per definition:

```python
        for definition in definitions:
            try:
                if definition.get("output_mode") == "structured" and definition.get("fields"):
                    response = self._call_structured(text, definition)
                    out.extend(self.parse_structured(response, definition))
                else:
                    response = self._call_llm(text, definition)
                    out.extend(self.parse_response(response, definition))
            except Exception as e:
                logger.warning("Insight '%s' extraction failed: %s", definition.get("name"), e)
```

Add `_call_structured` (mirrors `_call_llm` but uses the structured system prompt):

```python
    def _call_structured(self, transcript_text: str, definition: dict) -> str:
        config = self._summariser._config
        field_lines = "\n".join(
            f'- "{f["key"]}" ({f["type"]}): {f["label"]}' for f in definition.get("fields") or []
        )
        system = _STRUCTURED_SYSTEM_PROMPT.format(
            instruction=definition["prompt"], field_lines=field_lines
        )
        fence = "=" * 40
        user_msg = (
            f"Insight: {definition['name']}\n\n"
            f"{fence} BEGIN TRANSCRIPT {fence}\n{transcript_text}\n{fence} END TRANSCRIPT {fence}"
        )
        if config.backend == "claude":
            return self._summariser._claude_chat(system, user_msg)
        base_url = Summariser._validate_ollama_url(config.ollama_base_url)
        return self._summariser._ollama_chat(base_url, config.ollama_model, system, user_msg)
```

Ensure existing `parse_response` list items also include `"fields": None` for a uniform shape (add `"fields": None` to the dict it appends).

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_insights_structured.py -v && ruff check src/insights/`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/insights/extractor.py tests/test_insights_structured.py
git commit -m "feat(insights): structured extraction with typed-field coercion + rendering"
```

---

## Task 4: Insights route — accept & validate structured definitions

**Files:**

- Modify: `src/api/routes/insights.py`
- Test: `tests/test_insights_route_structured.py`

**Interfaces:**

- Consumes: `InsightRepository.create/update` structured params (Task 2).
- Produces: `POST`/`PATCH /api/insight-definitions` accept `output_mode` + `fields`; reject invalid field types / empty structured `fields` with HTTP 422.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_insights_route_structured.py
import pytest
from fastapi import HTTPException
from src.api.routes import insights as route


class _FakeRepo:
    def __init__(self):
        self.created = None
    async def create(self, **kw):
        self.created = kw
        return "id1"
    async def get(self, _id):
        return {"id": "id1", **(self.created or {})}


@pytest.fixture(autouse=True)
def _wire():
    fake = _FakeRepo()
    route.init(repo=object(), insight_repo=fake)
    return fake


async def test_create_structured_passes_fields(_wire):
    body = route.InsightCreate(
        name="Client Call", prompt="extract details", output_mode="structured",
        fields=[route.InsightField(key="go_live", label="Go-live", type="date")],
    )
    await route.create_insight_definition(body)
    assert _wire.created["output_mode"] == "structured"
    assert _wire.created["fields"][0]["type"] == "date"


def test_invalid_field_type_rejected():
    with pytest.raises(Exception):
        route.InsightField(key="x", label="X", type="banana")


async def test_structured_requires_nonempty_fields(_wire):
    body = route.InsightCreate(name="Bad", prompt="p", output_mode="structured", fields=[])
    with pytest.raises(HTTPException):
        await route.create_insight_definition(body)
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_insights_route_structured.py -v`
Expected: FAIL (`InsightField` undefined; models lack `output_mode`/`fields`).

- [ ] **Step 3: Implement models + validation**

In `src/api/routes/insights.py`:

```python
from typing import Literal

_FIELD_TYPES = ("text", "number", "date", "boolean", "list")


class InsightField(BaseModel):
    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=120)
    type: Literal["text", "number", "date", "boolean", "list"]


class InsightCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=2000)
    enabled: bool = True
    output_mode: Literal["list", "structured"] = "list"
    fields: list[InsightField] = Field(default_factory=list)


class InsightUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None
    output_mode: Literal["list", "structured"] | None = None
    fields: list[InsightField] | None = None


def _validate_structured(output_mode, fields) -> None:
    if output_mode == "structured":
        if not fields:
            raise HTTPException(422, "Structured insights require at least one field")
        keys = [f.key for f in fields]
        if len(keys) != len(set(keys)):
            raise HTTPException(422, "Field keys must be unique")
```

Update `create_insight_definition` and `update_insight_definition` to validate and pass through:

```python
@router.post("/api/insight-definitions", status_code=201)
async def create_insight_definition(body: InsightCreate):
    _require_repos()
    _validate_structured(body.output_mode, body.fields)
    insight_id = await _insight_repo.create(
        name=body.name.strip(), prompt=body.prompt.strip(), enabled=body.enabled,
        output_mode=body.output_mode,
        fields=[f.model_dump() for f in body.fields] if body.fields else None,
    )
    return await _insight_repo.get(insight_id)
```

For update, when `output_mode`/`fields` provided, validate the effective combination against the stored definition and pass `fields=[f.model_dump() ...]` (or `[]` to clear). Fetch existing first (already done for the 404 check) to resolve the effective mode.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_insights_route_structured.py -v && ruff check src/api/routes/insights.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/api/routes/insights.py tests/test_insights_route_structured.py
git commit -m "feat(api): insight definitions accept + validate structured typed fields"
```

---

## Task 5: Circleback-schema payload builder + HMAC signer

**Files:**

- Create: `src/automations/payload.py`
- Modify: `src/transcriber.py` (add `Transcript.from_dict`)
- Test: `tests/test_automation_payload.py`

**Interfaces:**

- Produces:
  - `Transcript.from_dict(data: dict) -> Transcript` (classmethod).
  - `sign_payload(body: bytes, secret: str) -> str` — hex HMAC-SHA256.
  - `build_circleback_payload(meeting, action_items, insights, *, include_transcript=False) -> dict`
    where `meeting` exposes attrs `id, title, started_at, ended_at, duration_seconds (optional), tags, attendees_json, summary_markdown, transcript_json`; `action_items` and `insights` are the repo dict lists.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_automation_payload.py
import hmac, hashlib, json
from types import SimpleNamespace
from src.automations.payload import build_circleback_payload, sign_payload
from src.transcriber import Transcript


def _meeting(**kw):
    base = dict(id="m1", title="Armacell UAT", started_at=1_700_000_000.0,
                ended_at=1_700_000_600.0, tags=["ClientX"],
                attendees_json='[{"name":"Jamie","email":"j@x.com"}]',
                summary_markdown="- did things", transcript_json=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_payload_has_circleback_field_names():
    p = build_circleback_payload(_meeting(), action_items=[], insights=[])
    assert p["id"] == "m1"
    assert p["name"] == "Armacell UAT"
    assert p["notes"] == "- did things"
    assert p["tags"] == ["ClientX"]
    assert p["attendees"] == [{"name": "Jamie", "email": "j@x.com"}]
    assert p["duration"] == 600.0
    assert "createdAt" in p and p["createdAt"].startswith("20")
    assert "transcript" not in p  # include_transcript defaults False


def test_action_item_status_mapped():
    items = [{"id": "a", "title": "T", "description": "D", "assignee": "Jamie",
              "status": "completed"},
             {"id": "b", "title": "U", "description": "", "assignee": "unassigned",
              "status": "open"},
             {"id": "c", "title": "V", "description": "", "assignee": None,
              "status": "cancelled"}]
    p = build_circleback_payload(_meeting(), action_items=items, insights=[])
    statuses = [ai["status"] for ai in p["actionItems"]]
    assert statuses == ["DONE", "PENDING"]  # cancelled omitted
    assert p["actionItems"][0]["assignee"] == {"name": "Jamie", "email": None}
    assert p["actionItems"][1]["assignee"] is None  # 'unassigned' -> null


def test_insights_grouped_list_and_structured():
    insights = [
        {"definition_name": "Questions", "content": "Is it live?", "speaker": "Sam", "fields": None},
        {"definition_name": "Client Call", "content": "Go-live: 2026-09-02",
         "speaker": "", "fields": {"go_live": "2026-09-02"}},
    ]
    p = build_circleback_payload(_meeting(), action_items=[], insights=insights)
    assert p["insights"]["Questions"] == [{"insight": "Is it live?", "speaker": "Sam"}]
    assert p["insights"]["Client Call"] == [{"insight": {"go_live": "2026-09-02"}, "speaker": None}]


def test_include_transcript_when_requested():
    tj = json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "hi", "speaker": "Sam"}]})
    p = build_circleback_payload(_meeting(transcript_json=tj), action_items=[], insights=[],
                                 include_transcript=True)
    assert p["transcript"] == [{"speaker": "Sam", "text": "hi", "timestamp": 1.0}]


def test_sign_payload_matches_hmac_sha256():
    body = b'{"a":1}'
    sig = sign_payload(body, "whsec_test")
    assert sig == hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()


def test_transcript_from_dict_roundtrip():
    t = Transcript.from_dict({"segments": [{"start": 1.0, "end": 2.0, "text": "hi", "speaker": "S"}],
                              "language": "en", "duration_seconds": 2.0})
    assert t.full_text == "hi"
    assert t.segments[0].speaker == "S"
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_automation_payload.py -v`
Expected: FAIL (module + classmethod missing).

- [ ] **Step 3: Add `Transcript.from_dict`**

In `src/transcriber.py`, on the `Transcript` dataclass:

```python
    @classmethod
    def from_dict(cls, data: dict) -> "Transcript":
        data = data or {}
        segs = [
            TranscriptSegment(
                start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                text=s.get("text", ""), speaker=s.get("speaker", ""),
            )
            for s in data.get("segments", [])
        ]
        return cls(
            segments=segs,
            language=data.get("language", ""),
            language_probability=data.get("language_probability", 0.0),
            duration_seconds=data.get("duration_seconds", 0.0),
        )
```

- [ ] **Step 4: Implement `src/automations/payload.py`**

```python
"""Pure builders for the Circleback-compatible webhook payload."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from src.automations.evaluator import domains_from_attendees  # reuse json parsing? no — inline
from src.transcriber import Transcript

_STATUS_MAP = {"open": "PENDING", "completed": "DONE"}


def sign_payload(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _attendees(attendees_json: str) -> list[dict]:
    try:
        raw = json.loads(attendees_json or "[]")
    except (ValueError, TypeError):
        return []
    out = []
    for e in raw if isinstance(raw, list) else []:
        if isinstance(e, dict):
            out.append({"name": e.get("name"), "email": e.get("email")})
        elif isinstance(e, str):
            out.append({"name": e, "email": None})
    return out


def _action_items(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        status = _STATUS_MAP.get(it.get("status", "open"))
        if status is None:  # e.g. 'cancelled' — omit
            continue
        assignee = it.get("assignee")
        assignee_obj = None
        if assignee and assignee != "unassigned":
            assignee_obj = {"name": assignee, "email": None}
        out.append({
            "id": it.get("id"), "title": it.get("title", ""),
            "description": it.get("description", ""),
            "assignee": assignee_obj, "status": status,
        })
    return out


def _insights(results: list[dict]) -> dict:
    grouped: dict[str, list] = {}
    for r in results:
        name = r.get("definition_name", "")
        if r.get("fields") is not None:
            entry = {"insight": r["fields"], "speaker": None}
        else:
            entry = {"insight": r.get("content", ""), "speaker": r.get("speaker") or None}
        grouped.setdefault(name, []).append(entry)
    return grouped


def _transcript(transcript_json) -> list[dict]:
    t = Transcript.from_dict(json.loads(transcript_json or "{}"))
    return [{"speaker": s.speaker, "text": s.text, "timestamp": s.start} for s in t.segments]


def _duration(meeting) -> float:
    d = getattr(meeting, "duration_seconds", None)
    if d:
        return float(d)
    started = getattr(meeting, "started_at", None)
    ended = getattr(meeting, "ended_at", None)
    if started and ended:
        return float(ended) - float(started)
    return 0.0


def build_circleback_payload(meeting, action_items, insights, *, include_transcript=False) -> dict:
    started = getattr(meeting, "started_at", None) or 0.0
    payload = {
        "id": getattr(meeting, "id", None),
        "name": getattr(meeting, "title", "") or "",
        "createdAt": datetime.fromtimestamp(float(started), tz=timezone.utc).isoformat(),
        "duration": _duration(meeting),
        "url": None,
        "tags": list(getattr(meeting, "tags", None) or []),
        "attendees": _attendees(getattr(meeting, "attendees_json", "") or ""),
        "notes": getattr(meeting, "summary_markdown", "") or "",
        "actionItems": _action_items(action_items or []),
        "insights": _insights(insights or []),
    }
    if include_transcript:
        payload["transcript"] = _transcript(getattr(meeting, "transcript_json", None))
    return payload
```

(Remove the unused `domains_from_attendees` import; it was a note. `_attendees` is self-contained.)

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_automation_payload.py -v && ruff check src/automations/ src/transcriber.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/automations/payload.py src/transcriber.py tests/test_automation_payload.py
git commit -m "feat(automations): Circleback-schema payload builder + HMAC signer + Transcript.from_dict"
```

---

## Task 6: ActionExecutor — run_insight + send_notes actions & wiring

**Files:**

- Modify: `src/automations/executor.py`
- Modify: `src/pipeline_runner.py` (`_run_automations` builds a services bundle)
- Test: `tests/test_automation_actions.py`

**Interfaces:**

- Consumes: `InsightRepository.replace_results_for_definition` (Task 2), `InsightExtractor` (Task 3), `build_circleback_payload`/`sign_payload` (Task 5), `Transcript.from_dict`.
- Produces: `ActionExecutor(repo, emit, services=None)` where `services` is a dict
  `{"meeting", "insight_repo", "action_items_repo", "summarisation_config"}`. New action types
  `run_insight` (params `definition_id`) and `send_notes` (params `url`, `include_transcript`, `secret`).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_automation_actions.py
import json
from types import SimpleNamespace
import pytest
from src.automations.executor import ActionExecutor


class _InsightRepo:
    def __init__(self, definition):
        self._def = definition
        self.written = None
    async def get(self, _id):
        return self._def
    async def replace_results_for_definition(self, meeting_id, definition_id, results):
        self.written = (meeting_id, definition_id, results)
        return len(results)
    async def results_for_meeting(self, _mid):
        return []


def _meeting(tj=None):
    return SimpleNamespace(id="m1", title="Armacell UAT", started_at=1.0, ended_at=61.0,
                           tags=[], attendees_json="[]", summary_markdown="notes",
                           transcript_json=tj or json.dumps({"segments": [
                               {"start": 0.0, "end": 5.0, "text": "hello world " * 20, "speaker": "S"}]}))


async def test_run_insight_writes_scoped_results(monkeypatch):
    definition = {"id": "d1", "name": "Client Call", "output_mode": "list", "fields": None,
                  "prompt": "list things"}
    irepo = _InsightRepo(definition)
    # Stub the extractor so no LLM is called.
    from src.insights import extractor as ext_mod
    monkeypatch.setattr(ext_mod.InsightExtractor, "extract",
                        lambda self, t, defs: [{"definition_id": "d1", "definition_name": "Client Call",
                                                "content": "x", "speaker": "", "fields": None}])
    services = {"meeting": _meeting(), "insight_repo": irepo,
                "action_items_repo": None, "summarisation_config": SimpleNamespace(backend="ollama")}
    ex = ActionExecutor(repo=None, emit=lambda *a, **k: None, services=services)
    rule = {"name": "R", "actions": [{"type": "run_insight", "definition_id": "d1"}]}
    await ex.run_rule(rule, context={"tags": []}, meeting_id="m1", run_side_effects=False)
    assert irepo.written[0] == "m1"
    assert irepo.written[1] == "d1"


async def test_send_notes_posts_signed_payload(monkeypatch):
    posted = {}
    async def fake_post(url, json_body, headers):
        posted["url"] = url; posted["body"] = json_body; posted["headers"] = headers
        return True
    ex = ActionExecutor(repo=None, emit=lambda *a, **k: None, services={
        "meeting": _meeting(), "insight_repo": _InsightRepo({}),
        "action_items_repo": SimpleNamespace(list_by_meeting=_aempty),
        "summarisation_config": None})
    monkeypatch.setattr(ex, "_post_json", fake_post)
    rule = {"name": "R", "actions": [
        {"type": "send_notes", "url": "https://x.test/hook", "secret": "whsec_1",
         "include_transcript": False}]}
    await ex.run_rule(rule, context={}, meeting_id="m1", run_side_effects=True)
    assert posted["url"] == "https://x.test/hook"
    assert posted["body"]["name"] == "Armacell UAT"
    assert "x-signature" in posted["headers"]


async def test_send_notes_skipped_when_not_side_effects(monkeypatch):
    called = []
    ex = ActionExecutor(repo=None, emit=lambda *a, **k: None, services={
        "meeting": _meeting(), "insight_repo": _InsightRepo({}),
        "action_items_repo": SimpleNamespace(list_by_meeting=_aempty), "summarisation_config": None})
    monkeypatch.setattr(ex, "_post_json", lambda *a, **k: called.append(1))
    rule = {"name": "R", "actions": [{"type": "send_notes", "url": "https://x", "secret": ""}]}
    await ex.run_rule(rule, context={}, meeting_id="m1", run_side_effects=False)
    assert called == []


async def _aempty(_mid):
    return []
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_automation_actions.py -v`
Expected: FAIL (`ActionExecutor` has no `services`, no new actions).

- [ ] **Step 3: Extend the executor**

In `src/automations/executor.py`:

```python
import asyncio
import json

import httpx

from src.automations.payload import build_circleback_payload, sign_payload
from src.insights.extractor import InsightExtractor
from src.transcriber import Transcript
# (keep existing imports: send_webhook, macos_send, WebhookChannelConfig)


class ActionExecutor:
    def __init__(self, repo, emit, services=None) -> None:
        self._repo = repo
        self._emit = emit
        self._services = services or {}

    async def run_rule(self, rule, context, meeting_id, *, run_side_effects) -> None:
        for action in rule.get("actions") or []:
            atype = action.get("type")
            try:
                if atype == "apply_tag":
                    await self._apply_tag(action, context, meeting_id)
                elif atype == "run_insight":
                    await self._run_insight(action, meeting_id)
                elif atype == "webhook" and run_side_effects:
                    await self._webhook(action, context, rule)
                elif atype == "send_notes" and run_side_effects:
                    await self._send_notes(action, meeting_id)
                elif atype == "notify" and run_side_effects:
                    await self._notify(action, context, rule, meeting_id)
            except Exception:
                logger.warning("Automation action %s failed for rule '%s'",
                               atype, rule.get("name"), exc_info=True)

    async def _run_insight(self, action, meeting_id) -> None:
        definition_id = action.get("definition_id")
        irepo = self._services.get("insight_repo")
        meeting = self._services.get("meeting")
        cfg = self._services.get("summarisation_config")
        if not (definition_id and irepo and meeting and cfg):
            return
        definition = await irepo.get(definition_id)
        if not definition or not definition.get("enabled", True):
            return
        transcript = Transcript.from_dict(json.loads(getattr(meeting, "transcript_json", None) or "{}"))
        extractor = InsightExtractor(cfg)
        results = await asyncio.to_thread(extractor.extract, transcript, [definition])
        await irepo.replace_results_for_definition(meeting_id, definition_id, results)

    async def _send_notes(self, action, meeting_id) -> None:
        url = action.get("url")
        if not url:
            return
        meeting = self._services.get("meeting")
        irepo = self._services.get("insight_repo")
        airepo = self._services.get("action_items_repo")
        action_items = await airepo.list_by_meeting(meeting_id) if airepo else []
        insights = await irepo.results_for_meeting(meeting_id) if irepo else []
        payload = build_circleback_payload(
            meeting, action_items, insights,
            include_transcript=bool(action.get("include_transcript")),
        )
        headers = {"Content-Type": "application/json"}
        secret = action.get("secret") or ""
        if secret:
            body = json.dumps(payload).encode("utf-8")
            headers["x-signature"] = sign_payload(body, secret)
        await self._post_json(url, payload, headers)

    async def _post_json(self, url, json_body, headers) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, json=json_body, headers=headers)
                resp.raise_for_status()
            return True
        except Exception as e:
            logger.warning("send_notes delivery failed: %s", e)
            return False
```

Keep `_apply_tag`, `_webhook`, `_notify` unchanged.

- [ ] **Step 4: Wire the services bundle in `pipeline_runner._run_automations`**

Replace `executor = ActionExecutor(self._db.repo, self._emit)` with:

```python
        from src.action_items.repository import ActionItemRepository
        from src.insights.repository import InsightRepository
        services = {
            "meeting": meeting,
            "insight_repo": InsightRepository(self._db.database),
            "action_items_repo": ActionItemRepository(self._db.database),
            "summarisation_config": self._config.summarisation,
        }
        executor = ActionExecutor(self._db.repo, self._emit, services=services)
```

(Confirm the action-items repository class name by reading `src/action_items/repository.py` — use the actual exported name.)

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_automation_actions.py -v && ruff check src/automations/ src/pipeline_runner.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/automations/executor.py src/pipeline_runner.py tests/test_automation_actions.py
git commit -m "feat(automations): run_insight + send_notes actions with services bundle"
```

---

## Task 7: Seed tailored starter content (idempotent)

**Files:**

- Create: `src/insights/seed.py`
- Modify: `src/api/server.py` (call once after repos init)
- Test: `tests/test_insights_seed.py`

**Interfaces:**

- Consumes: `InsightRepository.create` (structured), `AutomationRepository.create`, `MeetingRepository.get_meta/set_meta`.
- Produces: `async def seed_starter_content(meeting_repo, insight_repo, automation_repo) -> bool` — seeds once, guarded by `app_metadata['insights_seed_version']`; returns True if it seeded.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_insights_seed.py
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.insights.repository import InsightRepository
from src.automations.repository import AutomationRepository
from src.insights.seed import seed_starter_content, SEED_VERSION


async def _repos(tmp_path):
    db = Database(db_path=tmp_path / "seed.db")
    await db.connect()
    return db, MeetingRepository(db), InsightRepository(db), AutomationRepository(db)


async def test_seeds_structured_insights_and_rules(tmp_path):
    db, mrepo, irepo, arepo = await _repos(tmp_path)
    try:
        seeded = await seed_starter_content(mrepo, irepo, arepo)
        assert seeded is True
        defs = await irepo.list_definitions()
        names = {d["name"] for d in defs}
        assert {"Client Call Details", "Standup Snapshot", "Discovery Notes"} <= names
        client_call = next(d for d in defs if d["name"] == "Client Call Details")
        assert client_call["output_mode"] == "structured"
        assert any(f["key"] == "go_live_date" for f in client_call["fields"])
        rules = await arepo.list_rules()
        assert len(rules) >= 3
        # A rule references a real seeded definition via run_insight.
        run_ids = [a["definition_id"] for r in rules for a in r["actions"]
                   if a["type"] == "run_insight"]
        assert client_call["id"] in run_ids
        assert await mrepo.get_meta("insights_seed_version") == str(SEED_VERSION)
    finally:
        await db.close()


async def test_seed_is_idempotent(tmp_path):
    db, mrepo, irepo, arepo = await _repos(tmp_path)
    try:
        await seed_starter_content(mrepo, irepo, arepo)
        again = await seed_starter_content(mrepo, irepo, arepo)
        assert again is False
        assert len(await irepo.list_definitions()) == 3
    finally:
        await db.close()


async def test_seed_does_not_rerun_after_user_deletes(tmp_path):
    db, mrepo, irepo, arepo = await _repos(tmp_path)
    try:
        await seed_starter_content(mrepo, irepo, arepo)
        for d in await irepo.list_definitions():
            await irepo.delete(d["id"])
        await seed_starter_content(mrepo, irepo, arepo)
        assert await irepo.list_definitions() == []
    finally:
        await db.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_insights_seed.py -v`
Expected: FAIL (`src.insights.seed` missing).

- [ ] **Step 3: Implement `src/insights/seed.py`**

```python
"""One-time seeding of tailored starter insights + automation rules."""

import logging

logger = logging.getLogger("contextrecall.insights.seed")

SEED_VERSION = 1
_MARKER_KEY = "insights_seed_version"

_SEED_INSIGHTS = [
    {
        "name": "Client Call Details",
        "prompt": "Extract the key delivery details discussed with the client.",
        "fields": [
            {"key": "go_live_date", "label": "Go-live date", "type": "date"},
            {"key": "blockers", "label": "Blockers", "type": "list"},
            {"key": "risks", "label": "Risks", "type": "list"},
            {"key": "decisions", "label": "Decisions", "type": "list"},
            {"key": "owner_next_step", "label": "Owner / next step", "type": "text"},
        ],
    },
    {
        "name": "Standup Snapshot",
        "prompt": "Summarise the standup status across projects.",
        "fields": [
            {"key": "project_status", "label": "Per-project status", "type": "list"},
            {"key": "overdue_count", "label": "Overdue task count", "type": "number"},
            {"key": "absences", "label": "Absences & coverage", "type": "list"},
            {"key": "deadlines", "label": "Key deadlines", "type": "list"},
        ],
    },
    {
        "name": "Discovery Notes",
        "prompt": "Capture the discovery outcomes.",
        "fields": [
            {"key": "requirements", "label": "Requirements", "type": "list"},
            {"key": "open_questions", "label": "Open questions", "type": "list"},
            {"key": "scope_decisions", "label": "Scope decisions", "type": "list"},
            {"key": "compliance_flags", "label": "Compliance / PCI flags", "type": "text"},
        ],
    },
]

# (title substrings, insight name) — rules trigger on the title, any-match.
_SEED_RULES = [
    ("Client call auto-insight", ["uat", "client", "review"], "Client Call Details"),
    ("Standup auto-insight", ["catchup", "standup"], "Standup Snapshot"),
    ("Discovery auto-insight", ["discovery"], "Discovery Notes"),
]


async def seed_starter_content(meeting_repo, insight_repo, automation_repo) -> bool:
    if await meeting_repo.get_meta(_MARKER_KEY) is not None:
        return False
    name_to_id: dict[str, str] = {}
    for spec in _SEED_INSIGHTS:
        did = await insight_repo.create(
            name=spec["name"], prompt=spec["prompt"], enabled=True,
            output_mode="structured", fields=spec["fields"],
        )
        name_to_id[spec["name"]] = did
    for rule_name, substrings, insight_name in _SEED_RULES:
        did = name_to_id.get(insight_name)
        if not did:
            continue
        conditions = [{"field": "title_contains", "value": s} for s in substrings]
        await automation_repo.create(
            name=rule_name, match_mode="any", conditions=conditions,
            actions=[{"type": "run_insight", "definition_id": did}], enabled=True,
        )
    await meeting_repo.set_meta(_MARKER_KEY, str(SEED_VERSION))
    logger.info("Seeded %d starter insights + %d rules", len(_SEED_INSIGHTS), len(_SEED_RULES))
    return True
```

- [ ] **Step 4: Wire into boot (`src/api/server.py`)**

After the insights/automations repos are constructed (near line 189-195), schedule a one-shot seed on the API loop. Add, after the routers are wired:

```python
        # One-time starter content (idempotent; guarded by app_metadata marker).
        try:
            from src.insights.seed import seed_starter_content
            from src.automations.repository import AutomationRepository
            from src.insights.repository import InsightRepository
            await seed_starter_content(
                self.repo, InsightRepository(self.db), AutomationRepository(self.db)
            )
        except Exception:
            logger.warning("Starter-content seeding failed", exc_info=True)
```

(Place it in the async startup path where `await` is valid — mirror how existing startup coroutines run. If the surrounding function is sync, dispatch via the existing loop the same way other init coroutines are scheduled. Read the region first.)

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_insights_seed.py -v && ruff check src/insights/ src/api/server.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/insights/seed.py src/api/server.py tests/test_insights_seed.py
git commit -m "feat(insights): seed tailored structured insights + title-based automation rules"
```

---

## Task 8: UI types + API client

**Files:**

- Modify: `ui/src/lib/types.ts`, `ui/src/lib/api.ts`
- Test: `ui/src/lib/__tests__/api.test.ts` (extend)

**Interfaces:**

- Produces TS types consumed by Tasks 9–11:
  - `InsightField = { key: string; label: string; type: 'text'|'number'|'date'|'boolean'|'list' }`
  - `InsightDefinition` gains `output_mode: 'list'|'structured'` and `fields: InsightField[] | null`.
  - `InsightResult` gains `fields: Record<string, unknown> | null`.
  - `AutomationAction` union gains `{ type: 'run_insight'; definition_id: string }` and
    `{ type: 'send_notes'; url: string; include_transcript?: boolean; secret?: string }`.

- [ ] **Step 1: Write the failing test**

```typescript
// add to ui/src/lib/__tests__/api.test.ts
it("creates a structured insight definition with fields", async () => {
  fetchMock.mockResponseOnce(JSON.stringify({ id: "x" }));
  await api.createInsightDefinition({
    name: "Client Call",
    prompt: "p",
    enabled: true,
    output_mode: "structured",
    fields: [{ key: "go_live", label: "Go-live", type: "date" }],
  });
  const call = fetchMock.mock.calls.at(-1);
  const body = JSON.parse((call?.[1] as RequestInit).body as string);
  expect(body.output_mode).toBe("structured");
  expect(body.fields[0].type).toBe("date");
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts -t "structured insight"`
Expected: FAIL (createInsightDefinition signature lacks fields / type error).

- [ ] **Step 3: Extend types + api client**

In `ui/src/lib/types.ts`, extend the existing `InsightDefinition`, `InsightResult`, and `AutomationAction` types (read them first, add the fields above — do not remove existing members). In `ui/src/lib/api.ts`, widen `createInsightDefinition` / `updateInsightDefinition` payload types to include `output_mode` and `fields`.

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx vitest run src/lib/__tests__/api.test.ts && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/lib/types.ts ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts
git commit -m "feat(ui): types + api for structured insights and new automation actions"
```

---

## Task 9: InsightsSection — List/Structured toggle + field editor

**Files:**

- Modify: `ui/src/components/settings/InsightsSection.tsx`
- Test: `ui/src/components/settings/__tests__/InsightsSection.test.tsx` (extend)

**Interfaces:**

- Consumes: Task 8 types.
- Produces: when creating/editing a definition, a mode selector; in structured mode, add/remove field rows (label + type). `key` is derived from `label` via slugify (`label.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "")`). Submits `output_mode` + `fields`.

- [ ] **Step 1: Write the failing test**

```tsx
it("lets the user add a typed field in structured mode and submits it", async () => {
  render(<InsightsSection />); // follow the file's existing render/setup helpers
  await userEvent.click(screen.getByRole("button", { name: /new insight/i }));
  await userEvent.click(screen.getByLabelText(/structured/i));
  await userEvent.click(screen.getByRole("button", { name: /add field/i }));
  await userEvent.type(
    screen.getByPlaceholderText(/field label/i),
    "Go-live date",
  );
  await userEvent.selectOptions(screen.getByLabelText(/field type/i), "date");
  // ...fill name/prompt per existing test patterns, submit
  await userEvent.click(screen.getByRole("button", { name: /save|create/i }));
  expect(createMock).toHaveBeenCalledWith(
    expect.objectContaining({
      output_mode: "structured",
      fields: [
        expect.objectContaining({
          key: "go_live_date",
          label: "Go-live date",
          type: "date",
        }),
      ],
    }),
  );
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/components/settings/__tests__/InsightsSection.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement the toggle + field-row editor**

Read `InsightsSection.tsx`; add local state `outputMode` and `fields: InsightField[]`. Render a mode radio/select; when `structured`, render a list of field rows each with a label input (`placeholder="Field label"`), a type `<select aria-label="Field type">` over the five types, and a remove button, plus an "Add field" button. Derive `key` on submit. Include `output_mode` + `fields` in the create/update payload. Keep list-mode behaviour identical when `outputMode === 'list'`.

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx vitest run src/components/settings/__tests__/InsightsSection.test.tsx && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/settings/InsightsSection.tsx ui/src/components/settings/__tests__/InsightsSection.test.tsx
git commit -m "feat(ui): structured-insight field editor in settings"
```

---

## Task 10: MeetingInsights — render structured results

**Files:**

- Modify: `ui/src/components/meetings/MeetingInsights.tsx`
- Test: `ui/src/components/meetings/__tests__/MeetingInsightResults.test.tsx` (extend)

**Interfaces:**

- Consumes: `InsightResult.fields` (Task 8).
- Produces: a structured result (`fields != null`) renders as a labelled key→value card; list results (`fields == null`) render as today. `null`/empty values show as `—`.

- [ ] **Step 1: Write the failing test**

```tsx
it("renders a structured insight as labelled fields", () => {
  render(
    <MeetingInsights
      results={[
        {
          definition_id: "d",
          definition_name: "Client Call Details",
          content: "Go-live: 2026-09-02",
          speaker: "",
          fields: {
            "Go-live date": "2026-09-02",
            Blockers: ["A", "B"],
            "Owner / next step": null,
          },
        },
      ]}
    />,
  );
  expect(screen.getByText("Go-live date")).toBeInTheDocument();
  expect(screen.getByText("2026-09-02")).toBeInTheDocument();
  expect(screen.getByText("A; B")).toBeInTheDocument();
  expect(screen.getByText("—")).toBeInTheDocument();
});
```

Note: the result `fields` is keyed by field `key`. The card should show the field **label**. If the component only receives `key`s, render the key humanised, or (preferred) join against the definition's `fields`. For the test above, pass a display-ready map; adapt to the component's actual props when implementing.

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/MeetingInsightResults.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement structured rendering**

Read `MeetingInsights.tsx`. For each result, branch: if `result.fields` is non-null, render a definition-titled card with one row per field (`label` + value; arrays joined with `; `; `null`/`""`/`[]` → `—`); else render the existing list item. Group by `definition_name` as the component already does.

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx vitest run src/components/meetings/__tests__/MeetingInsightResults.test.tsx && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/meetings/MeetingInsights.tsx ui/src/components/meetings/__tests__/MeetingInsightResults.test.tsx
git commit -m "feat(ui): render structured insight results as labelled cards"
```

---

## Task 11: AutomationsSection — run_insight + send_notes action editors

**Files:**

- Modify: `ui/src/components/settings/AutomationsSection.tsx`
- Test: `ui/src/components/settings/__tests__/AutomationsSection.test.tsx` (extend)

**Interfaces:**

- Consumes: Task 8 action types; the insight-definitions list (already fetched elsewhere or fetch here).
- Produces: action-type options include **Run insight** (a `<select>` of definitions → `definition_id`) and **Send notes to webhook** (`url` text, `secret` text, `include_transcript` checkbox). Existing `apply_tag`/`webhook`/`notify` editors unchanged.

- [ ] **Step 1: Write the failing test**

```tsx
it("configures a run_insight action referencing a definition", async () => {
  render(<AutomationsSection />); // follow existing setup; mock definitions list to include {id:"d1", name:"Client Call Details"}
  await userEvent.click(
    screen.getByRole("button", { name: /new automation|add rule/i }),
  );
  await userEvent.selectOptions(
    screen.getByLabelText(/action type/i),
    "run_insight",
  );
  await userEvent.selectOptions(screen.getByLabelText(/insight/i), "d1");
  // fill name + a condition per existing patterns, submit
  await userEvent.click(screen.getByRole("button", { name: /save|create/i }));
  expect(createRuleMock).toHaveBeenCalledWith(
    expect.objectContaining({
      actions: [
        expect.objectContaining({ type: "run_insight", definition_id: "d1" }),
      ],
    }),
  );
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd ui && npx vitest run src/components/settings/__tests__/AutomationsSection.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement the two action editors**

Read `AutomationsSection.tsx`. Extend the action-type `<select>` with `run_insight` and `send_notes`. For `run_insight`, render an insight `<select aria-label="Insight">` populated from the definitions query (add a `getInsightDefinitions` fetch if not present). For `send_notes`, render `url`, `secret`, and an `include_transcript` checkbox. Serialize into the action object shapes from Task 8.

- [ ] **Step 4: Run to verify pass**

Run: `cd ui && npx vitest run src/components/settings/__tests__/AutomationsSection.test.tsx && npx tsc --noEmit`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ui/src/components/settings/AutomationsSection.tsx ui/src/components/settings/__tests__/AutomationsSection.test.tsx
git commit -m "feat(ui): run_insight + send_notes automation action editors"
```

---

## Task 12: Render insights into markdown + Notion export

**Files:**

- Modify: `src/output/markdown_writer.py`, `src/output/notion_writer.py`
- Test: `tests/test_markdown_writer_insights.py` (create) + extend Notion writer test if one exists.

**Interfaces:**

- Consumes: `InsightRepository.results_for_meeting` shape (`content`, `fields`, `definition_name`).
- Produces: an "Insights" section in the exported markdown; a corresponding Notion block group. If the writers already take a meeting dict, thread `insights` in via the caller (`pipeline_runner` write step). First read the writers to see whether they already receive insights; only add if absent.

- [ ] **Step 1: Investigate current behaviour**

Read `src/output/markdown_writer.py` and the write call site in `src/pipeline_runner.py`. Determine whether insights are already passed/rendered. If they are, this task reduces to structured rendering only.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_markdown_writer_insights.py
from src.output.markdown_writer import render_insights_section  # add this pure helper


def test_render_insights_section_lists_and_structured():
    results = [
        {"definition_name": "Questions", "content": "Is it live?", "fields": None},
        {"definition_name": "Client Call Details", "content": "Go-live: 2026-09-02",
         "fields": {"go_live_date": "2026-09-02"}},
    ]
    md = render_insights_section(results)
    assert "## Insights" in md
    assert "Questions" in md
    assert "Is it live?" in md
    assert "Client Call Details" in md
    assert "2026-09-02" in md


def test_render_insights_section_empty_is_blank():
    assert render_insights_section([]) == ""
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 -m pytest tests/test_markdown_writer_insights.py -v`
Expected: FAIL (`render_insights_section` missing).

- [ ] **Step 4: Implement the pure renderer + wire it in**

Add `render_insights_section(results)` to `markdown_writer.py` (heading `## Insights`, one sub-list per `definition_name`, list results as bullets of `content`, structured results as bullets of `content` — already human-readable). Append its output to the meeting markdown when insights exist. Thread `insights=await insight_repo.results_for_meeting(meeting_id)` from the write step in `pipeline_runner` (insights are extracted at step 5, before the async writer? — if writers run in the synchronous `_process` before post-processing, fetch insights at render time from the repo instead, or move insight rendering to a post-write refresh). Choose the path that reads insights **after** they exist; document the ordering decision in the commit message. Do the equivalent block rendering for `notion_writer.py`.

- [ ] **Step 5: Run to verify pass**

Run: `python3 -m pytest tests/test_markdown_writer_insights.py -v && ruff check src/output/`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/output/ tests/test_markdown_writer_insights.py
git commit -m "feat(output): render insights (list + structured) into markdown + Notion export"
```

---

## Final verification

- [ ] **Full Python suite:** `python3 -m pytest tests/ -q` → all green.
- [ ] **Lint:** `ruff check src/ tests/` → clean.
- [ ] **UI suite:** `cd ui && npm test` → all green.
- [ ] **Type check:** `cd ui && npx tsc --noEmit` → clean.
- [ ] **Rust unaffected:** no changes; skip unless CI requires.
- [ ] Update `CLAUDE.md` route list + `SCHEMA_VERSION` note (25→27 routers already existed; note v23).

---

## Self-Review Notes (spec coverage)

- Structured insights (spec Part A): Tasks 1–4, 9, 10, 12. ✅
- Automations depth (spec Part B): Tasks 5, 6, 11. ✅
- Seeded content (spec Part C): Task 7. ✅
- Circleback webhook schema + HMAC: Task 5 (+6 delivery). ✅
- Migration v23 mirrors `test_db_migration_v20.py`: Task 1. ✅
- Reprocess safety (`run_insight` ungated, `send_notes` gated): Task 6 tests. ✅
- Out-of-scope items (native Slack/Linear, attendee email, timestamps, nested conditions): not planned. ✅
