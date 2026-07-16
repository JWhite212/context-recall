"""
Custom insight endpoints.

GET    /api/insight-definitions            — list definitions
POST   /api/insight-definitions            — create
PATCH  /api/insight-definitions/{id}       — update (name/prompt/enabled/output_mode/fields)
DELETE /api/insight-definitions/{id}       — delete (results preserved)
GET    /api/meetings/{id}/insights         — extracted results for a meeting
"""

import logging
from typing import Literal

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


@router.get("/api/insight-definitions")
async def list_insight_definitions():
    _require_repos()
    return await _insight_repo.list_definitions()


@router.post("/api/insight-definitions", status_code=201)
async def create_insight_definition(body: InsightCreate):
    _require_repos()
    _validate_structured(body.output_mode, body.fields)
    insight_id = await _insight_repo.create(
        name=body.name.strip(),
        prompt=body.prompt.strip(),
        enabled=body.enabled,
        output_mode=body.output_mode,
        fields=[f.model_dump() for f in body.fields] if body.fields else None,
    )
    return await _insight_repo.get(insight_id)


@router.patch("/api/insight-definitions/{insight_id}")
async def update_insight_definition(insight_id: str, body: InsightUpdate):
    _require_repos()
    existing = await _insight_repo.get(insight_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Insight not found")

    effective_mode = (
        body.output_mode if body.output_mode is not None else existing.get("output_mode", "list")
    )
    if body.fields is not None:
        effective_fields = body.fields
    else:
        effective_fields = [InsightField(**f) for f in (existing.get("fields") or [])]
    _validate_structured(effective_mode, effective_fields)

    await _insight_repo.update(
        insight_id,
        name=body.name.strip() if body.name is not None else None,
        prompt=body.prompt.strip() if body.prompt is not None else None,
        enabled=body.enabled,
        output_mode=body.output_mode,
        fields=[f.model_dump() for f in body.fields] if body.fields is not None else None,
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
