"""
Keyword tracker endpoints.

GET    /api/trackers                     — list trackers
POST   /api/trackers                     — create
PATCH  /api/trackers/{id}                — update (name/keywords/enabled)
DELETE /api/trackers/{id}                — delete (hits cascade)
GET    /api/trackers/{id}/hits           — recent hits for a tracker
GET    /api/meetings/{id}/tracker-hits   — hits inside one meeting
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("contextrecall.api.trackers")

router = APIRouter()

_repo = None  # MeetingRepository
_tracker_repo = None  # TrackerRepository


def init(repo, tracker_repo) -> None:
    global _repo, _tracker_repo
    _repo = repo
    _tracker_repo = tracker_repo


def _require_repos() -> None:
    if not _repo or not _tracker_repo:
        raise HTTPException(status_code=503, detail="Repository not available")


class TrackerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(min_length=1, max_length=50)
    enabled: bool = True


class TrackerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    keywords: list[str] | None = Field(default=None, max_length=50)
    enabled: bool | None = None


def _clean_keywords(keywords: list[str]) -> list[str]:
    cleaned = [k.strip() for k in keywords if k and len(k.strip()) >= 2]
    if not cleaned:
        raise HTTPException(
            status_code=422, detail="At least one keyword of 2+ characters required"
        )
    return cleaned


@router.get("/api/trackers")
async def list_trackers():
    _require_repos()
    return await _tracker_repo.list_trackers()


@router.post("/api/trackers", status_code=201)
async def create_tracker(body: TrackerCreate):
    _require_repos()
    tracker_id = await _tracker_repo.create(
        name=body.name.strip(),
        keywords=_clean_keywords(body.keywords),
        enabled=body.enabled,
    )
    return await _tracker_repo.get(tracker_id)


@router.patch("/api/trackers/{tracker_id}")
async def update_tracker(tracker_id: str, body: TrackerUpdate):
    _require_repos()
    if not await _tracker_repo.get(tracker_id):
        raise HTTPException(status_code=404, detail="Tracker not found")
    await _tracker_repo.update(
        tracker_id,
        name=body.name.strip() if body.name is not None else None,
        keywords=_clean_keywords(body.keywords) if body.keywords is not None else None,
        enabled=body.enabled,
    )
    return await _tracker_repo.get(tracker_id)


@router.delete("/api/trackers/{tracker_id}")
async def delete_tracker(tracker_id: str):
    _require_repos()
    if not await _tracker_repo.delete(tracker_id):
        raise HTTPException(status_code=404, detail="Tracker not found")
    return {"deleted": tracker_id}


@router.get("/api/trackers/{tracker_id}/hits")
async def tracker_hits(tracker_id: str, limit: int = 200):
    _require_repos()
    if not await _tracker_repo.get(tracker_id):
        raise HTTPException(status_code=404, detail="Tracker not found")
    return await _tracker_repo.hits_for_tracker(tracker_id, limit=max(1, min(limit, 500)))


@router.get("/api/meetings/{meeting_id}/tracker-hits")
async def meeting_tracker_hits(meeting_id: str):
    _require_repos()
    if not await _repo.get_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    return await _tracker_repo.hits_for_meeting(meeting_id)
