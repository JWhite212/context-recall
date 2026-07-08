# Custom Insights Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** User-defined "insights" — named LLM extractions (e.g. Risks, Decisions) that produce a list of items per meeting, managed in Settings and shown on the meeting, reprocess-safe.

**Architecture:** `InsightRepository` (definitions + per-meeting results, reprocess-safe `replace_results_for_meeting`) mirrors `TrackerRepository`. `InsightExtractor` (dynamic prompt built from each definition, fenced-JSON parse, non-fatal) mirrors `ActionItemExtractor`. A `_extract_insights` stage runs in `_post_process_async`. New `insights` API router (mirrors `trackers.py`) + a Settings `InsightsSection` (mirrors `TemplatesSection`) + results in `MeetingInsights.tsx`.

**Tech Stack:** Python 3.11, pytest + pytest-asyncio, aiosqlite, FastAPI; React 19 + TS + Vitest.

## Global Constraints

- **British spelling** in new comments/logs.
- **Head `SCHEMA_VERSION` is 15** here → new tables at **v16**. Add a `if current_version < 16:` block _after_ the v15 block and **move the trailing `else:`** (currently on the v15 block) to the v16 block. Incremental blocks use a **literal** `PRAGMA user_version = 16` (not `{SCHEMA_VERSION}`). Also add the two `executescript`s to the fresh-install (`< 1`) block.
- **Distinct naming** — the existing `meeting_insights` route owns `/api/meetings/{id}/talk-stats` + `/draft-email`; this feature uses module `insights`, routes `/api/insight-definitions*` + `GET /api/meetings/{id}/insights`. Do not touch `meeting_insights`.
- **Reprocess model:** always `replace_results_for_meeting` (unconditional, like trackers) — no `is_reprocess` branch needed.
- **LLM calls off the loop:** always `await asyncio.to_thread(...)` in the pipeline (blocking HTTP).
- **Results survive definition deletion:** `insight_results` denormalises `definition_name`; `meeting_id` has an `ON DELETE CASCADE` FK, `definition_id` does **not** (deleting a definition keeps historical results).
- Lint `ruff check src/ tests/`; UI `cd ui && npx tsc --noEmit`. Commits end with the `Claude-Session:` trailer.

---

### Task 1: DB v16 — insight tables + migration

**Files:** Modify `src/db/database.py`; Test `tests/test_db_migration_v16.py` (create)

**Interfaces:** Produces tables `insight_definitions(id,name,prompt,enabled,created_at,updated_at)` and `insight_results(id,definition_id,definition_name,meeting_id,content,speaker,created_at)`.

- [ ] **Step 1: Failing test** — `tests/test_db_migration_v16.py`:

```python
import aiosqlite

from src.db.database import SCHEMA_VERSION, Database


async def test_v16_creates_insight_tables(tmp_path):
    db = Database(db_path=tmp_path / "v16.db")
    await db.connect()
    try:
        assert SCHEMA_VERSION >= 16
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('insight_definitions','insight_results') ORDER BY name"
        )
        assert [r[0] for r in await cur.fetchall()] == [
            "insight_definitions",
            "insight_results",
        ]
    finally:
        await db.close()


async def test_v16_upgrade_from_v15_preserves_data(tmp_path):
    db_path = tmp_path / "v15old.db"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "CREATE TABLE meetings (id TEXT PRIMARY KEY, started_at REAL)"
        )
        await conn.execute("INSERT INTO meetings (id, started_at) VALUES ('m1', 1.0)")
        await conn.execute("PRAGMA user_version = 15")
        await conn.commit()
    db = Database(db_path=db_path)
    await db.connect()
    try:
        cur = await db.conn.execute("PRAGMA user_version")
        assert (await cur.fetchone())[0] == SCHEMA_VERSION
        cur = await db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='insight_definitions'"
        )
        assert await cur.fetchone() is not None
        cur = await db.conn.execute("SELECT id FROM meetings WHERE id='m1'")
        assert await cur.fetchone() is not None
    finally:
        await db.close()
```

- [ ] **Step 2: Run — expect FAIL** (`SCHEMA_VERSION` 15, tables absent):
      `.venv/bin/python -m pytest tests/test_db_migration_v16.py -v`

- [ ] **Step 3: Implement** in `src/db/database.py`:

1. `SCHEMA_VERSION = 15` → `16`.
2. Add the table SQL near `TRACKERS_SQL`:

```python
INSIGHT_DEFINITIONS_SQL = """
CREATE TABLE IF NOT EXISTS insight_definitions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

INSIGHT_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS insight_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    definition_id TEXT NOT NULL,
    definition_name TEXT NOT NULL,
    meeting_id TEXT NOT NULL,
    content TEXT NOT NULL,
    speaker TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_insight_results_meeting ON insight_results(meeting_id);
CREATE INDEX IF NOT EXISTS idx_insight_results_definition ON insight_results(definition_id);
"""
```

3. In the fresh-install block, before its `PRAGMA user_version = {SCHEMA_VERSION}` line, add:

```python
            await self.conn.executescript(INSIGHT_DEFINITIONS_SQL)
            await self.conn.executescript(INSIGHT_RESULTS_SQL)
```

4. Replace the v15 block's trailing `else:` with a new v16 block. The current tail is:

```python
            logger.info("Database migrated to version 15 (per-meeting template)")
            current_version = 15
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

→

```python
            logger.info("Database migrated to version 15 (per-meeting template)")
            current_version = 15

        if current_version < 16:
            # Custom insights: user-defined LLM extractions.
            await self.conn.executescript(INSIGHT_DEFINITIONS_SQL)
            await self.conn.executescript(INSIGHT_RESULTS_SQL)
            await self.conn.execute("PRAGMA user_version = 16")
            await self.conn.commit()
            logger.info("Database migrated to version 16 (custom insights)")
            current_version = 16
        else:
            logger.debug("Database schema up to date (version %d)", current_version)
```

- [ ] **Step 4: Run — expect PASS** (+ v14/v15 still green): `.venv/bin/python -m pytest tests/test_db_migration_v16.py tests/test_db_migration_v15.py tests/test_db_migration_v14.py -q`
- [ ] **Step 5: Commit** — `git add src/db/database.py tests/test_db_migration_v16.py && git commit -m "feat(db): insight_definitions + insight_results tables (v16)"`

---

### Task 2: `InsightRepository`

**Files:** Create `src/insights/__init__.py`, `src/insights/repository.py`; Test `tests/test_insights_repository.py`

**Interfaces (Produces):** `InsightRepository(db)` with `create(name, prompt, enabled=True) -> str`, `get(id) -> dict|None`, `update(id, *, name=None, prompt=None, enabled=None)`, `list_definitions(enabled_only=False) -> list[dict]`, `delete(id) -> bool`, `replace_results_for_meeting(meeting_id, results: list[dict]) -> int` (each result `{definition_id, definition_name, content, speaker}`), `results_for_meeting(meeting_id) -> list[dict]`.

- [ ] **Step 1: Failing test** — `tests/test_insights_repository.py`:

```python
import pytest

from src.insights.repository import InsightRepository


@pytest.fixture
async def insight_repo(db):
    return InsightRepository(db)


@pytest.mark.asyncio
async def test_definition_crud(insight_repo):
    did = await insight_repo.create(name="Risks", prompt="List risks raised.")
    d = await insight_repo.get(did)
    assert d["name"] == "Risks"
    assert d["prompt"] == "List risks raised."
    assert d["enabled"] is True
    await insight_repo.update(did, enabled=False, name="Risks & blockers")
    d = await insight_repo.get(did)
    assert d["enabled"] is False
    assert d["name"] == "Risks & blockers"
    assert await insight_repo.list_definitions(enabled_only=True) == []
    assert len(await insight_repo.list_definitions()) == 1
    assert await insight_repo.delete(did) is True
    assert await insight_repo.get(did) is None


@pytest.mark.asyncio
async def test_replace_results_is_reprocess_safe(insight_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    did = await insight_repo.create(name="Risks", prompt="p")
    first = [
        {"definition_id": did, "definition_name": "Risks", "content": "a", "speaker": ""},
        {"definition_id": did, "definition_name": "Risks", "content": "b", "speaker": "Me"},
    ]
    assert await insight_repo.replace_results_for_meeting(mid, first) == 2
    assert await insight_repo.replace_results_for_meeting(mid, first[:1]) == 1
    results = await insight_repo.results_for_meeting(mid)
    assert len(results) == 1
    assert results[0]["definition_name"] == "Risks"


@pytest.mark.asyncio
async def test_results_survive_definition_delete(insight_repo, repo):
    mid = await repo.create_meeting(started_at=1000.0, status="complete")
    did = await insight_repo.create(name="Risks", prompt="p")
    await insight_repo.replace_results_for_meeting(
        mid, [{"definition_id": did, "definition_name": "Risks", "content": "a", "speaker": ""}]
    )
    await insight_repo.delete(did)
    results = await insight_repo.results_for_meeting(mid)
    assert len(results) == 1
    assert results[0]["definition_name"] == "Risks"
```

- [ ] **Step 2: Run — expect FAIL** (module missing): `.venv/bin/python -m pytest tests/test_insights_repository.py -q`

- [ ] **Step 3: Implement** — `src/insights/__init__.py` (empty) and `src/insights/repository.py` (mirror `TrackerRepository`):

```python
"""Data access for custom insight definitions and their per-meeting results."""

import logging
import time
import uuid

from src.db.database import Database

logger = logging.getLogger("contextrecall.insights")


class InsightRepository:
    """Async CRUD for insight_definitions + insight_results."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, name: str, prompt: str, enabled: bool = True) -> str:
        insight_id = str(uuid.uuid4())
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "INSERT INTO insight_definitions "
                "(id, name, prompt, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (insight_id, name, prompt, 1 if enabled else 0, now, now),
            )
            await self._db.conn.commit()
        return insight_id

    async def update(self, insight_id, *, name=None, prompt=None, enabled=None) -> None:
        sets, vals = [], []
        if name is not None:
            sets.append("name = ?")
            vals.append(name)
        if prompt is not None:
            sets.append("prompt = ?")
            vals.append(prompt)
        if enabled is not None:
            sets.append("enabled = ?")
            vals.append(1 if enabled else 0)
        if not sets:
            return
        sets.append("updated_at = ?")
        vals.append(time.time())
        vals.append(insight_id)
        async with self._db.write_lock:
            await self._db.conn.execute(
                f"UPDATE insight_definitions SET {', '.join(sets)} WHERE id = ?", vals
            )
            await self._db.conn.commit()

    async def get(self, insight_id: str) -> dict | None:
        cur = await self._db.conn.execute(
            "SELECT * FROM insight_definitions WHERE id = ?", (insight_id,)
        )
        row = await cur.fetchone()
        return self._row_to_dict(row) if row else None

    async def list_definitions(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM insight_definitions"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY created_at"
        cur = await self._db.conn.execute(sql)
        return [self._row_to_dict(r) for r in await cur.fetchall()]

    async def delete(self, insight_id: str) -> bool:
        async with self._db.write_lock:
            cur = await self._db.conn.execute(
                "DELETE FROM insight_definitions WHERE id = ?", (insight_id,)
            )
            await self._db.conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row_to_dict(row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "prompt": row["prompt"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    async def replace_results_for_meeting(self, meeting_id: str, results: list[dict]) -> int:
        now = time.time()
        async with self._db.write_lock:
            await self._db.conn.execute(
                "DELETE FROM insight_results WHERE meeting_id = ?", (meeting_id,)
            )
            for r in results:
                await self._db.conn.execute(
                    "INSERT INTO insight_results "
                    "(definition_id, definition_name, meeting_id, content, speaker, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        r["definition_id"],
                        r["definition_name"],
                        meeting_id,
                        r["content"],
                        r.get("speaker", ""),
                        now,
                    ),
                )
            await self._db.conn.commit()
        return len(results)

    async def results_for_meeting(self, meeting_id: str) -> list[dict]:
        cur = await self._db.conn.execute(
            "SELECT definition_id, definition_name, content, speaker "
            "FROM insight_results WHERE meeting_id = ? ORDER BY id",
            (meeting_id,),
        )
        return [
            {
                "definition_id": r["definition_id"],
                "definition_name": r["definition_name"],
                "content": r["content"],
                "speaker": r["speaker"],
            }
            for r in await cur.fetchall()
        ]
```

> Confirm `self._db.write_lock` and `self._db.conn` are what `TrackerRepository` uses (read `src/trackers/repository.py` first; match exactly — it uses `async with self._db.write_lock:` and `self._db.conn`).

- [ ] **Step 4: Run — expect PASS**: `.venv/bin/python -m pytest tests/test_insights_repository.py -q`
- [ ] **Step 5: Lint + commit** — `ruff check`; `git add src/insights/ tests/test_insights_repository.py && git commit -m "feat(insights): InsightRepository (definitions + reprocess-safe results)"`

---

### Task 3: `InsightExtractor`

**Files:** Create `src/insights/extractor.py`; Test `tests/test_insights_extractor.py`

**Interfaces (Produces):** `InsightExtractor(summarisation_config)` with `extract(transcript, definitions: list[dict]) -> list[dict]` (flat list of `{definition_id, definition_name, content, speaker}`) and a sync `parse_response(response, definition) -> list[dict]`.

- [ ] **Step 1: Failing test** — `tests/test_insights_extractor.py` (test `parse_response` directly, no LLM, mirroring `test_action_items_extractor.py`):

````python
import pytest

from src.insights.extractor import InsightExtractor
from src.utils.config import SummarisationConfig

_DEF = {"id": "d1", "name": "Risks", "prompt": "List risks."}


@pytest.fixture
def extractor():
    return InsightExtractor(SummarisationConfig(backend="ollama"))


def test_parses_json_list(extractor):
    items = extractor.parse_response(
        '[{"content": "vendor lock-in", "speaker": "Me"}, {"content": "timeline slip"}]',
        _DEF,
    )
    assert [i["content"] for i in items] == ["vendor lock-in", "timeline slip"]
    assert items[0]["definition_id"] == "d1"
    assert items[0]["definition_name"] == "Risks"
    assert items[0]["speaker"] == "Me"
    assert items[1]["speaker"] == ""


def test_parses_markdown_fenced(extractor):
    items = extractor.parse_response('```json\n[{"content": "a"}]\n```', _DEF)
    assert len(items) == 1 and items[0]["content"] == "a"


def test_malformed_returns_empty(extractor):
    assert extractor.parse_response("not json at all", _DEF) == []


def test_empty_returns_empty(extractor):
    assert extractor.parse_response("", _DEF) == []


def test_drops_items_without_content(extractor):
    items = extractor.parse_response('[{"speaker": "Me"}, {"content": "ok"}]', _DEF)
    assert [i["content"] for i in items] == ["ok"]
````

- [ ] **Step 2: Run — expect FAIL**: `.venv/bin/python -m pytest tests/test_insights_extractor.py -q`

- [ ] **Step 3: Implement** — `src/insights/extractor.py` (mirror `ActionItemExtractor`; dynamic prompt per definition):

````python
"""LLM extraction of user-defined insights from meeting transcripts."""

import json
import logging
import re

from src.summariser import Summariser
from src.transcriber import Transcript
from src.utils.config import SummarisationConfig

logger = logging.getLogger("contextrecall.insights.extractor")

_SYSTEM_PROMPT = """You extract a specific kind of information from a meeting transcript.

The user wants: {instruction}

Return ONLY a JSON array. Each element is an object:
- "content": one concise item (a short phrase or sentence)
- "speaker": the speaker's name if attributable, else null

Return only genuine items. If there are none, return an empty array: []
No explanation, no markdown."""

_MAX_WORDS = 10000


class InsightExtractor:
    """Runs one LLM call per insight definition, returning list items."""

    def __init__(self, summarisation_config: SummarisationConfig) -> None:
        self._summariser = Summariser(summarisation_config)

    def extract(self, transcript: Transcript, definitions: list[dict]) -> list[dict]:
        text = transcript.full_text
        if not text or len(text.split()) < 10:
            return []
        words = text.split()
        if len(words) > _MAX_WORDS:
            text = " ".join(words[:5000]) + "\n...\n" + " ".join(words[-5000:])
        out: list[dict] = []
        for definition in definitions:
            try:
                response = self._call_llm(text, definition)
                out.extend(self.parse_response(response, definition))
            except Exception as e:
                logger.warning("Insight '%s' extraction failed: %s", definition.get("name"), e)
        return out

    def _call_llm(self, transcript_text: str, definition: dict) -> str:
        config = self._summariser._config
        system = _SYSTEM_PROMPT.format(instruction=definition["prompt"])
        fence = "=" * 40
        user_msg = (
            f"Insight: {definition['name']}\n\n"
            f"{fence} BEGIN TRANSCRIPT {fence}\n{transcript_text}\n{fence} END TRANSCRIPT {fence}"
        )
        if config.backend == "claude":
            return self._summariser._claude_chat(system, user_msg)
        base_url = Summariser._validate_ollama_url(config.ollama_base_url)
        return self._summariser._ollama_chat(base_url, config.ollama_model, system, user_msg)

    def parse_response(self, response: str, definition: dict) -> list[dict]:
        if not response:
            return []
        cleaned = response.strip()
        cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
        try:
            items = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", cleaned, re.DOTALL)
            if not match:
                return []
            try:
                items = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            speaker = item.get("speaker")
            out.append({
                "definition_id": definition["id"],
                "definition_name": definition["name"],
                "content": content,
                "speaker": str(speaker).strip() if speaker else "",
            })
        return out
````

- [ ] **Step 4: Run — expect PASS**: `.venv/bin/python -m pytest tests/test_insights_extractor.py -q`
- [ ] **Step 5: Lint + commit** — `git add src/insights/extractor.py tests/test_insights_extractor.py && git commit -m "feat(insights): InsightExtractor LLM list extraction"`

---

### Task 4: Config `InsightsConfig`

**Files:** Modify `src/utils/config.py`, `config.example.yaml`; Test `tests/test_config.py`

- [ ] **Step 1: Failing test** (add to `tests/test_config.py`):

```python
def test_insights_config_defaults():
    from src.utils.config import InsightsConfig

    cfg = InsightsConfig()
    assert cfg.enabled is True
    assert cfg.auto_extract is True
```

- [ ] **Step 2: Run — expect FAIL**: `.venv/bin/python -m pytest tests/test_config.py -k insights_config -q`
- [ ] **Step 3: Implement** — add near `ActionItemsConfig`:

```python
@dataclass
class InsightsConfig:
    enabled: bool = True
    auto_extract: bool = True
```

Wire into `AppConfig` (after `tagging`): `insights: InsightsConfig = field(default_factory=InsightsConfig)`; and into `load_config`'s `AppConfig(...)` call (after `tagging=...`): `insights=_build_dataclass(InsightsConfig, raw.get("insights", {})),`. Add a commented block to `config.example.yaml`:

```yaml
# --- Custom Insights ---
insights:
  # Extract user-defined insights (Settings → Insights) from each meeting.
  # enabled: true
  # auto_extract: true
```

- [ ] **Step 4: Run — expect PASS**: `.venv/bin/python -m pytest tests/test_config.py -q`
- [ ] **Step 5: Commit** — `git add src/utils/config.py config.example.yaml tests/test_config.py && git commit -m "feat(config): InsightsConfig"`

---

### Task 5: Pipeline `_extract_insights`

**Files:** Modify `src/pipeline_runner.py`; Test `tests/test_pipeline_runner.py`

**Interfaces (Consumes):** `InsightRepository`, `InsightExtractor`, `self._config.insights`.

- [ ] **Step 1: Failing test** (add to `tests/test_pipeline_runner.py`, mirroring the action-items post-processing test):

```python
def test_post_processing_extracts_insights(tmp_path, loop_thread):
    repo = _make_repo()
    bridge = DbBridge(repo, loop_thread, database=MagicMock())
    config = _make_config(tmp_path)
    config.insights.enabled = True
    config.insights.auto_extract = True
    runner = _make_runner(config, db=bridge)
    ins_repo = MagicMock()
    ins_repo.list_definitions = AsyncMock(return_value=[{"id": "d1", "name": "Risks", "prompt": "p"}])
    ins_repo.replace_results_for_meeting = AsyncMock()
    with (
        patch("src.insights.extractor.InsightExtractor") as ext_cls,
        patch("src.insights.repository.InsightRepository", return_value=ins_repo),
        patch("src.analytics.engine.AnalyticsEngine") as engine_cls,
    ):
        ext_cls.return_value.extract.return_value = [
            {"definition_id": "d1", "definition_name": "Risks", "content": "a", "speaker": ""}
        ]
        engine_cls.return_value.refresh_period = AsyncMock()
        asyncio.run(runner._post_process_async("m1", _make_transcript(), started_at=1000.0, is_reprocess=False))
    ins_repo.replace_results_for_meeting.assert_awaited_once()
```

Also set `config.insights.enabled = False` in `_make_config` (like `action_items.auto_extract = False`) so other pipeline tests don't trigger insight extraction — add `config.insights.auto_extract = False` there.

- [ ] **Step 2: Run — expect FAIL**: `.venv/bin/python -m pytest tests/test_pipeline_runner.py -k extracts_insights -q`

- [ ] **Step 3: Implement** — in `_post_process_async`, add a guarded block alongside the others:

```python
        try:
            insights_cfg = getattr(self._config, "insights", None)
            if insights_cfg and insights_cfg.enabled and insights_cfg.auto_extract:
                await self._extract_insights(meeting_id, transcript)
        except Exception:
            logger.warning("Insight extraction failed", exc_info=True)
```

Add the method (mirror `_scan_trackers` — unconditional replace):

```python
    async def _extract_insights(self, meeting_id: str, transcript) -> None:
        from src.insights.extractor import InsightExtractor
        from src.insights.repository import InsightRepository

        if self._db.database is None:
            return
        repo = InsightRepository(self._db.database)
        definitions = await repo.list_definitions(enabled_only=True)
        results = []
        if definitions:
            extractor = InsightExtractor(self._config.summarisation)
            # Blocking HTTP — keep it off the API event loop.
            results = await asyncio.to_thread(extractor.extract, transcript, definitions)
        # Always replace — a reprocess (or a since-disabled definition) must
        # clear stale rows even when nothing new is extracted.
        await repo.replace_results_for_meeting(meeting_id, results)
        if results:
            self._emit("insights.extracted", meeting_id=meeting_id, count=len(results))
```

- [ ] **Step 4: Run — expect PASS** (+ full runner suite): `.venv/bin/python -m pytest tests/test_pipeline_runner.py -q`
- [ ] **Step 5: Commit** — `git add src/pipeline_runner.py tests/test_pipeline_runner.py && git commit -m "feat(pipeline): extract custom insights in post-processing"`

---

### Task 6: API route + server registration

**Files:** Create `src/api/routes/insights.py`; Modify `src/api/server.py`; Test `tests/test_api_insights.py`

**Interfaces (Produces):** `GET/POST /api/insight-definitions`, `PATCH/DELETE /api/insight-definitions/{id}`, `GET /api/meetings/{id}/insights`.

- [ ] **Step 1: Failing test** — `tests/test_api_insights.py` (clone `test_api_trackers.py`):

```python
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import src.api.auth as auth_mod
from src.api.auth import verify_token
from src.api.routes import insights as insights_routes
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.insights.repository import InsightRepository

TEST_TOKEN = "test-token-for-insights"


@pytest.fixture(autouse=True)
def _patch_auth():
    original = auth_mod._auth_token
    auth_mod._auth_token = TEST_TOKEN
    yield
    auth_mod._auth_token = original


def _auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
async def api(tmp_path):
    db = Database(db_path=tmp_path / "insights_api.db")
    await db.connect()
    repo = MeetingRepository(db)
    insight_repo = InsightRepository(db)
    insights_routes.init(repo, insight_repo)
    app = FastAPI()
    app.include_router(insights_routes.router, dependencies=[Depends(verify_token)])
    yield {"app": app, "db": db, "repo": repo, "insight_repo": insight_repo}
    await db.close()


@pytest.mark.asyncio
async def test_insight_definition_lifecycle(api):
    with TestClient(api["app"]) as c:
        created = c.post(
            "/api/insight-definitions",
            headers=_auth_headers(),
            json={"name": "Risks", "prompt": "List risks."},
        )
        assert created.status_code == 201
        did = created.json()["id"]
        patched = c.patch(
            f"/api/insight-definitions/{did}", headers=_auth_headers(), json={"enabled": False}
        )
        assert patched.json()["enabled"] is False
        assert c.delete(f"/api/insight-definitions/{did}", headers=_auth_headers()).status_code == 200
        assert c.get("/api/insight-definitions", headers=_auth_headers()).json() == []


@pytest.mark.asyncio
async def test_meeting_insights_404_for_unknown_meeting(api):
    with TestClient(api["app"]) as c:
        assert c.get("/api/meetings/nope/insights", headers=_auth_headers()).status_code == 404
```

- [ ] **Step 2: Run — expect FAIL**: `.venv/bin/python -m pytest tests/test_api_insights.py -q`

- [ ] **Step 3: Implement** — `src/api/routes/insights.py` (mirror `trackers.py`):

```python
"""
Custom insight endpoints.

GET    /api/insight-definitions            — list definitions
POST   /api/insight-definitions            — create
PATCH  /api/insight-definitions/{id}       — update (name/prompt/enabled)
DELETE /api/insight-definitions/{id}       — delete (results preserved)
GET    /api/meetings/{id}/insights         — extracted results for a meeting
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("contextrecall.api.insights")

router = APIRouter()

_repo = None  # MeetingRepository
_insight_repo = None  # InsightRepository


def init(repo, insight_repo) -> None:
    global _repo, _insight_repo
    _repo = repo
    _insight_repo = insight_repo


def _require_repos() -> None:
    if not _repo or not _insight_repo:
        raise HTTPException(status_code=503, detail="Repository not available")


class InsightCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class InsightUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, min_length=1, max_length=2000)
    enabled: bool | None = None


@router.get("/api/insight-definitions")
async def list_insight_definitions():
    _require_repos()
    return await _insight_repo.list_definitions()


@router.post("/api/insight-definitions", status_code=201)
async def create_insight_definition(body: InsightCreate):
    _require_repos()
    insight_id = await _insight_repo.create(
        name=body.name.strip(), prompt=body.prompt.strip(), enabled=body.enabled
    )
    return await _insight_repo.get(insight_id)


@router.patch("/api/insight-definitions/{insight_id}")
async def update_insight_definition(insight_id: str, body: InsightUpdate):
    _require_repos()
    if not await _insight_repo.get(insight_id):
        raise HTTPException(status_code=404, detail="Insight not found")
    await _insight_repo.update(
        insight_id,
        name=body.name.strip() if body.name is not None else None,
        prompt=body.prompt.strip() if body.prompt is not None else None,
        enabled=body.enabled,
    )
    return await _insight_repo.get(insight_id)


@router.delete("/api/insight-definitions/{insight_id}")
async def delete_insight_definition(insight_id: str):
    _require_repos()
    if not await _insight_repo.delete(insight_id):
        raise HTTPException(status_code=404, detail="Insight not found")
    return {"deleted": insight_id}


@router.get("/api/meetings/{meeting_id}/insights")
async def meeting_insights(meeting_id: str):
    _require_repos()
    if not await _repo.get_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    return await _insight_repo.results_for_meeting(meeting_id)
```

Register in `src/api/server.py` mirroring trackers (read the exact block first): add `from src.api.routes import insights as insights_routes` with the other route imports; `from src.insights.repository import InsightRepository` + `insights_routes.init(self.repo, InsightRepository(self.db))` in the main wiring block; and `app.include_router(insights_routes.router, dependencies=auth_deps)` after `auth_deps` is defined.

- [ ] **Step 4: Run — expect PASS**: `.venv/bin/python -m pytest tests/test_api_insights.py -q`
- [ ] **Step 5: Lint + commit** — `git add src/api/routes/insights.py src/api/server.py tests/test_api_insights.py && git commit -m "feat(api): custom insight definition CRUD + meeting results"`

---

### Task 7: UI types + API client

**Files:** Modify `ui/src/lib/types.ts`, `ui/src/lib/api.ts`; Test `ui/src/lib/__tests__/api.test.ts`

- [ ] **Step 1: Failing test** (add to `api.test.ts`, matching its fetch-spy style):

```ts
it("createInsightDefinition POSTs the body", async () => {
  const calls: { url: string; init?: RequestInit }[] = [];
  globalThis.fetch = vi.fn(
    async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: input.toString(), init });
      return new Response(JSON.stringify({ id: "d1" }), {
        status: 201,
        headers: { "content-type": "application/json" },
      });
    },
  ) as unknown as typeof fetch;
  await createInsightDefinition({ name: "Risks", prompt: "p" });
  const call = calls.find((c) => c.init?.method === "POST");
  expect(call?.url).toContain("/api/insight-definitions");
  expect(JSON.parse(call?.init?.body as string)).toEqual({
    name: "Risks",
    prompt: "p",
  });
});
```

Add `createInsightDefinition` to the test's imports.

- [ ] **Step 2: Run — expect FAIL**: `cd ui && npx vitest run src/lib/__tests__/api.test.ts`

- [ ] **Step 3: Implement** — in `types.ts` add:

```ts
export interface InsightDefinition {
  id: string;
  name: string;
  prompt: string;
  enabled: boolean;
  created_at: number;
  updated_at: number;
}

export interface MeetingInsightResult {
  definition_id: string;
  definition_name: string;
  content: string;
  speaker: string;
}
```

In `api.ts` (mirror the tracker fns; add `InsightDefinition, MeetingInsightResult` to the type import block):

```ts
export async function getInsightDefinitions(): Promise<InsightDefinition[]> {
  return request<InsightDefinition[]>("/api/insight-definitions");
}

export async function createInsightDefinition(def: {
  name: string;
  prompt: string;
  enabled?: boolean;
}): Promise<InsightDefinition> {
  return request<InsightDefinition>("/api/insight-definitions", {
    method: "POST",
    body: JSON.stringify(def),
  });
}

export async function updateInsightDefinition(
  id: string,
  fields: Partial<Pick<InsightDefinition, "name" | "prompt" | "enabled">>,
): Promise<InsightDefinition> {
  return request<InsightDefinition>(
    `/api/insight-definitions/${encodeURIComponent(id)}`,
    {
      method: "PATCH",
      body: JSON.stringify(fields),
    },
  );
}

export async function deleteInsightDefinition(id: string): Promise<void> {
  await request(`/api/insight-definitions/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function getMeetingInsights(
  meetingId: string,
): Promise<MeetingInsightResult[]> {
  return request<MeetingInsightResult[]>(
    `/api/meetings/${encodeURIComponent(meetingId)}/insights`,
  );
}
```

- [ ] **Step 4: Run — expect PASS + tsc**: `cd ui && npx vitest run src/lib/__tests__/api.test.ts && npx tsc --noEmit`
- [ ] **Step 5: Commit** — `git add ui/src/lib/types.ts ui/src/lib/api.ts ui/src/lib/__tests__/api.test.ts && git commit -m "feat(ui): insight-definition API client + types"`

---

### Task 8: Settings — Insights management panel

**Files:** Modify `ui/src/components/settings/Settings.tsx`; Test `ui/src/components/settings/__tests__/InsightsSection.test.tsx` (create)

- [ ] **Step 1:** Read `Settings.tsx` `TemplatesSection` (~508-836), the `SETTINGS_SECTIONS` array (~28-45), the `Section`/`Field`/`Toggle`/`FORM_INPUT` primitives (~51-78), and the render site (~2029). The `InsightsSection` mirrors `TemplatesSection` exactly but with fields **name** (text) + **prompt** (textarea) + **enabled** (`Toggle`), backed by `useQuery(["insight-definitions"], getInsightDefinitions)` + `useMutation`s for create/update/delete invalidating `["insight-definitions"]`.

- [ ] **Step 2: Failing test** — extract the panel as `InsightsSection` and test it renders + create calls the API. `ui/src/components/settings/__tests__/InsightsSection.test.tsx`: wrap in `QueryClientProvider` (retry:false) + `ToastProvider`, stub `fetch` to return `[{id,name,prompt,enabled}]` for GET `/api/insight-definitions`, render `<InsightsSection id="insights" />`, `await waitFor` the existing definition's name to appear. (Follow the Feature-1 `TemplateBadge`/`useDaemonStatus` wrapper pattern; keep the assertion to a stable rendered value, not a brittle interaction.)

- [ ] **Step 3: Run — expect FAIL**: `cd ui && npx vitest run src/components/settings/__tests__/InsightsSection.test.tsx`

- [ ] **Step 4: Implement** — add `{ id: "insights", label: "Insights" }` to `SETTINGS_SECTIONS`; add an exported `InsightsSection({ id })` component (mirror `TemplatesSection`, reusing `Section`/`Field`/`Toggle`/`FORM_INPUT`) with the name+prompt+enabled CRUD; render `{daemonRunning && <InsightsSection id="insights" />}` near the `TemplatesSection` render site. Import the four api fns.

- [ ] **Step 5: Run — expect PASS + tsc + full UI suite**: `cd ui && npx vitest run src/components/settings/__tests__/InsightsSection.test.tsx && npx tsc --noEmit && npm test`
- [ ] **Step 6: Commit** — `git add ui/src/components/settings/ && git commit -m "feat(ui): Settings panel to manage custom insights"`

---

### Task 9: Meeting insight results

**Files:** Modify `ui/src/components/meetings/MeetingInsights.tsx`; Test `ui/src/components/meetings/__tests__/MeetingInsightResults.test.tsx` (create)

- [ ] **Step 1:** Read `MeetingInsights.tsx` (fetch + grouped-pill region ~37-116). Add a `useQuery(["meeting-insights", meetingId], () => getMeetingInsights(meetingId))` and render results **grouped by `definition_name`** as a labelled list. To keep the test robust, extract the grouped-list rendering into a small presentational `InsightResults({ results })` component (like Feature 1's `TemplateBadge`) and render it inside `MeetingInsights`.

- [ ] **Step 2: Failing test** — `MeetingInsightResults.test.tsx`: pure `render(<InsightResults results={[{definition_name:"Risks",content:"a",...},{definition_name:"Risks",content:"b",...},{definition_name:"Decisions",content:"c",...}]} />)`; assert "Risks" and "Decisions" group headers render and all three contents appear.

- [ ] **Step 3: Run — expect FAIL**: `cd ui && npx vitest run src/components/meetings/__tests__/MeetingInsightResults.test.tsx`

- [ ] **Step 4: Implement** — create `InsightResults` (groups `results` by `definition_name`, renders each group with its items; returns null when empty), and render it in `MeetingInsights` fed by the new query. Update the `MeetingInsights` early-return gate so it doesn't hide when only insight results exist.

- [ ] **Step 5: Run — expect PASS + tsc + full UI suite**: `cd ui && npx vitest run src/components/meetings/__tests__/MeetingInsightResults.test.tsx && npx tsc --noEmit && npm test`
- [ ] **Step 6: Commit** — `git add ui/src/components/meetings/ && git commit -m "feat(ui): show custom insight results on the meeting"`

---

### Task 10: Feature verification

- [ ] `.venv/bin/python -m pytest tests/ -q` → all pass.
- [ ] `.venv/bin/ruff check src/ tests/` → clean.
- [ ] `cd ui && npm test && npx tsc --noEmit` → pass.
- [ ] `coderabbit review --agent --base origin/main` → address Critical/Warning, re-run until clean.
- [ ] Report per-task test counts + anything not done.

---

## Self-Review

**Spec coverage:** definitions+results tables/migration (T1) ✔; `InsightRepository` reprocess-safe (T2) ✔; `InsightExtractor` list extraction (T3) ✔; config gate (T4) ✔; pipeline stage (T5) ✔; API CRUD + meeting results, distinct from `meeting_insights` (T6) ✔; UI types/client (T7), Settings panel (T8), meeting results (T9) ✔; testing throughout ✔. "Results survive definition deletion" → T1 (no FK cascade on `definition_id`) + T2 test.

**Placeholder scan:** new files (repository, extractor, route, config, migration, api fns) are complete code. Four steps say "read the exact region first" (repo `write_lock`/`conn` confirm, server.py block, Settings `TemplatesSection`, MeetingInsights region) — each names the file + what to match, actionable not vague. UI Tasks 8–9 extract small presentational units to test (matching the Feature-1 decision to avoid brittle full renders).

**Type consistency:** result dict shape `{definition_id, definition_name, content, speaker}` is identical across extractor (T3), repository (T2), pipeline (T5), API (T6), and UI `MeetingInsightResult` (T7/T9). `InsightRepository`/`InsightExtractor`/`InsightsConfig` names consistent across tasks. Routes `/api/insight-definitions*` + `/api/meetings/{id}/insights` consistent between T6 and T7.
