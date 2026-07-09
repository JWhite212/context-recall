"""
Automation rule endpoints.

GET    /api/automation-rules          — list rules
POST   /api/automation-rules          — create
PATCH  /api/automation-rules/{id}     — update
DELETE /api/automation-rules/{id}     — delete (dispatch history preserved)
GET    /api/meetings/{id}/automations — rules that fired for a meeting
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("contextrecall.api.automations")

router = APIRouter()

_repo = None  # MeetingRepository
_auto_repo = None  # AutomationRepository


def init(repo, auto_repo) -> None:
    global _repo, _auto_repo
    _repo = repo
    _auto_repo = auto_repo


def _require_repos() -> None:
    if not _repo or not _auto_repo:
        raise HTTPException(status_code=503, detail="Repository not available")


class Condition(BaseModel):
    field: Literal["tag", "client", "project", "title_contains", "attendee_domain"]
    value: str = Field(min_length=1, max_length=500)


class Action(BaseModel):
    type: Literal["apply_tag", "webhook", "notify"]
    tags: list[str] | None = None
    url: str | None = None
    format: str | None = None
    message: str | None = None

    @model_validator(mode="after")
    def _require_type_params(self) -> "Action":
        # Reject semantically-invalid actions at the edge: an apply_tag with
        # no tags or a webhook with no URL would silently never do anything.
        if self.type == "apply_tag" and not [t for t in (self.tags or []) if t.strip()]:
            raise ValueError("apply_tag requires at least one non-empty tag")
        if self.type == "webhook" and not (self.url or "").strip():
            raise ValueError("webhook requires a url")
        return self


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    match_mode: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(min_length=1)
    actions: list[Action] = Field(min_length=1)
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    match_mode: Literal["all", "any"] | None = None
    conditions: list[Condition] | None = Field(default=None, min_length=1)
    actions: list[Action] | None = Field(default=None, min_length=1)
    enabled: bool | None = None


@router.get("/api/automation-rules")
async def list_rules():
    _require_repos()
    return await _auto_repo.list_rules()


@router.post("/api/automation-rules", status_code=201)
async def create_rule(body: RuleCreate):
    _require_repos()
    rule_id = await _auto_repo.create(
        name=body.name.strip(),
        match_mode=body.match_mode,
        conditions=[c.model_dump() for c in body.conditions],
        actions=[a.model_dump(exclude_none=True) for a in body.actions],
        enabled=body.enabled,
    )
    return await _auto_repo.get(rule_id)


@router.patch("/api/automation-rules/{rule_id}")
async def update_rule(rule_id: str, body: RuleUpdate):
    _require_repos()
    if not await _auto_repo.get(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    await _auto_repo.update(
        rule_id,
        name=body.name.strip() if body.name is not None else None,
        match_mode=body.match_mode,
        conditions=(
            [c.model_dump() for c in body.conditions] if body.conditions is not None else None
        ),
        actions=(
            [a.model_dump(exclude_none=True) for a in body.actions]
            if body.actions is not None
            else None
        ),
        enabled=body.enabled,
    )
    return await _auto_repo.get(rule_id)


@router.delete("/api/automation-rules/{rule_id}")
async def delete_rule(rule_id: str):
    _require_repos()
    if not await _auto_repo.delete(rule_id):
        raise HTTPException(status_code=404, detail="Rule not found")
    return {"deleted": rule_id}


@router.get("/api/meetings/{meeting_id}/automations")
async def meeting_automations(meeting_id: str):
    _require_repos()
    if not await _repo.get_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    return await _auto_repo.fired_rules_for_meeting(meeting_id)
