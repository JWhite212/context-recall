"""API routes for meeting prep briefings."""

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from src.prep.briefing import PrepBriefingGenerator
from src.prep.repository import PrepRepository
from src.prep.sweep import event_signature

router = APIRouter(prefix="/api/prep", tags=["prep"])
_repo: PrepRepository | None = None
_generator: PrepBriefingGenerator | None = None


def init(repo: PrepRepository, generator: PrepBriefingGenerator | None = None) -> None:
    global _repo, _generator
    _repo = repo
    _generator = generator


def _get_repo() -> PrepRepository:
    if _repo is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _repo


@router.get("/upcoming-list")
async def get_upcoming_list(limit: int = 20):
    return await _get_repo().list_upcoming(limit)


@router.get("/prepared-events")
async def get_prepared_events():
    return {"event_uids": await _get_repo().prepared_event_uids()}


class _Attendee(BaseModel):
    name: str = ""
    email: str = ""


class _GenerateEventBody(BaseModel):
    event_uid: str = Field(min_length=1)
    title: str = ""
    attendees: list[_Attendee] = Field(default_factory=list)
    attendee_names: list[str] = Field(default_factory=list)
    end_ts: float
    series_id: str | None = None


@router.get("/by-event/{event_uid}")
async def get_briefing_by_event(event_uid: str, response: Response):
    briefing = await _get_repo().get_by_calendar_event(event_uid)
    if not briefing:
        response.status_code = 204
        return None
    return briefing


@router.post("/by-event/generate", status_code=201)
async def generate_briefing_by_event(body: _GenerateEventBody):
    if not _generator:
        raise HTTPException(status_code=503, detail="Briefing generator not available")
    emails = [a.email for a in body.attendees if a.email]
    sig = event_signature(emails)
    await _generator.generate(
        title=body.title,
        attendees=emails,
        attendee_names=body.attendee_names,
        series_id=body.series_id,
        calendar_event_uid=body.event_uid,
        event_signature=sig,
        expires_at=body.end_ts,
    )
    return await _get_repo().get_by_calendar_event(body.event_uid)


@router.get("/{meeting_id}")
async def get_briefing(meeting_id: str):
    briefing = await _get_repo().get_by_meeting(meeting_id)
    if not briefing:
        raise HTTPException(status_code=404, detail="No briefing found")
    return briefing


@router.post("/{meeting_id}/generate", status_code=201)
async def generate_briefing(meeting_id: str):
    if not _generator:
        raise HTTPException(status_code=503, detail="Briefing generator not available")
    briefing_id = await _generator.generate(
        title="Manual prep",
        attendees=[],
        attendee_names=[],
        meeting_id=meeting_id,
    )
    briefing = await _get_repo().get(briefing_id)
    return briefing
