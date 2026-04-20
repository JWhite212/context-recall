"""API routes for notification management."""

from fastapi import APIRouter
from pydantic import BaseModel

from src.notifications.repository import NotificationRepository

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
_repo: NotificationRepository | None = None


def init(repo: NotificationRepository) -> None:
    global _repo
    _repo = repo


class DismissRequest(BaseModel):
    status: str = "dismissed"


@router.get("")
async def list_notifications(limit: int = 50, offset: int = 0, status: str | None = None):
    items = await _repo.list_notifications(limit=limit, offset=offset, status=status)
    return {"notifications": items}


@router.get("/unread-count")
async def unread_count():
    count = await _repo.count_unread()
    return {"count": count}


@router.patch("/{notif_id}")
async def dismiss_notification(notif_id: str, body: DismissRequest):
    await _repo.dismiss(notif_id)
    return {"status": "dismissed"}
