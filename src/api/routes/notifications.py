"""API routes for notification management."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.notifications.repository import NotificationRepository

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
_repo: NotificationRepository | None = None


def init(repo: NotificationRepository) -> None:
    global _repo
    _repo = repo


def _get_repo() -> NotificationRepository:
    if _repo is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _repo


class DismissRequest(BaseModel):
    status: str = "dismissed"


@router.get("")
async def list_notifications(limit: int = 50, offset: int = 0, status: str | None = None):
    items = await _get_repo().list_notifications(limit=limit, offset=offset, status=status)
    return {"notifications": items}


@router.get("/unread-count")
async def unread_count():
    count = await _get_repo().count_unread()
    return {"count": count}


@router.patch("/{notif_id}")
async def dismiss_notification(notif_id: str, body: DismissRequest):
    await _get_repo().dismiss(notif_id, status=body.status)
    return {"status": body.status}
