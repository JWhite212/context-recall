"""API routes for meeting prep briefings."""

from fastapi import APIRouter, HTTPException, Response

from src.prep.briefing import PrepBriefingGenerator
from src.prep.repository import PrepRepository

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


@router.get("/upcoming")
async def get_upcoming(response: Response):
    briefing = await _get_repo().get_upcoming()
    if not briefing:
        response.status_code = 204
        return None
    return briefing


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
