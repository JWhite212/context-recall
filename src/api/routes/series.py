"""API routes for meeting series management."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.series.repository import SeriesRepository

router = APIRouter(prefix="/api/series", tags=["series"])
_repo: SeriesRepository | None = None


def init(repo: SeriesRepository) -> None:
    global _repo
    _repo = repo


class CreateSeriesRequest(BaseModel):
    title: str
    calendar_series_id: str | None = None


class UpdateSeriesRequest(BaseModel):
    title: str | None = None
    typical_day_of_week: int | None = None
    typical_time: str | None = None
    typical_duration_minutes: int | None = None


class LinkMeetingRequest(BaseModel):
    meeting_id: str


@router.get("")
async def list_series():
    return {"series": await _repo.list_all()}


@router.get("/{series_id}")
async def get_series(series_id: str):
    series = await _repo.get(series_id)
    if not series:
        raise HTTPException(status_code=404, detail="Series not found")
    series["meetings"] = await _repo.get_meetings(series_id)
    return series


@router.post("", status_code=201)
async def create_series(body: CreateSeriesRequest):
    series_id = await _repo.create(
        title=body.title, detection_method="manual", calendar_series_id=body.calendar_series_id
    )
    return await _repo.get(series_id)


@router.patch("/{series_id}")
async def update_series(series_id: str, body: UpdateSeriesRequest):
    if not await _repo.get(series_id):
        raise HTTPException(status_code=404, detail="Series not found")
    fields = body.model_dump(exclude_none=True)
    if fields:
        await _repo.update(series_id, **fields)
    return await _repo.get(series_id)


@router.delete("/{series_id}", status_code=204)
async def delete_series(series_id: str):
    if not await _repo.get(series_id):
        raise HTTPException(status_code=404, detail="Series not found")
    await _repo.delete(series_id)


@router.post("/{series_id}/meetings", status_code=201)
async def link_meeting(series_id: str, body: LinkMeetingRequest):
    if not await _repo.get(series_id):
        raise HTTPException(status_code=404, detail="Series not found")
    await _repo.link_meeting(body.meeting_id, series_id)
    return {"status": "linked"}


@router.get("/{series_id}/trends")
async def get_trends(series_id: str):
    if not await _repo.get(series_id):
        raise HTTPException(status_code=404, detail="Series not found")
    meetings = await _repo.get_meetings(series_id)
    durations = [m["duration_seconds"] for m in meetings if m.get("duration_seconds")]
    word_counts = [m["word_count"] for m in meetings if m.get("word_count")]
    return {
        "series_id": series_id,
        "meeting_count": len(meetings),
        "duration_trend": durations[-10:],
        "word_count_trend": word_counts[-10:],
        "avg_duration_minutes": (sum(durations) / len(durations) / 60) if durations else 0,
    }
