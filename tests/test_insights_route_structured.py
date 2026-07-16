import pytest
from fastapi import HTTPException

from src.api.routes import insights as route


class _FakeRepo:
    def __init__(self, existing=None):
        self.created = None
        self.updated = None
        self._existing = existing or {}

    async def create(self, **kw):
        self.created = kw
        return "id1"

    async def update(self, insight_id, **kw):
        self.updated = kw
        self._existing = {**self._existing, **{k: v for k, v in kw.items() if v is not None}}

    async def get(self, _id):
        if self.created is not None:
            return {"id": "id1", **self.created}
        return {"id": "id1", "output_mode": "list", "fields": None, **self._existing}


@pytest.fixture(autouse=True)
def _wire():
    fake = _FakeRepo()
    route.init(repo=object(), insight_repo=fake)
    return fake


async def test_create_structured_passes_fields(_wire):
    body = route.InsightCreate(
        name="Client Call",
        prompt="extract details",
        output_mode="structured",
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


async def test_update_structured_passes_fields_through():
    fake = _FakeRepo(existing={"output_mode": "list", "fields": None})
    route.init(repo=object(), insight_repo=fake)
    body = route.InsightUpdate(
        output_mode="structured",
        fields=[route.InsightField(key="status", label="Status", type="text")],
    )
    await route.update_insight_definition("id1", body)
    assert fake.updated["output_mode"] == "structured"
    assert fake.updated["fields"][0]["type"] == "text"


async def test_update_clearing_fields_while_structured_rejected():
    fake = _FakeRepo(
        existing={
            "output_mode": "structured",
            "fields": [{"key": "status", "label": "Status", "type": "text"}],
        }
    )
    route.init(repo=object(), insight_repo=fake)
    body = route.InsightUpdate(fields=[])
    with pytest.raises(HTTPException):
        await route.update_insight_definition("id1", body)
