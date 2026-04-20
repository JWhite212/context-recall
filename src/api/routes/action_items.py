"""API routes for action item management."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.action_items.repository import ActionItemRepository

router = APIRouter(tags=["action-items"])
_repo: ActionItemRepository | None = None


def init(repo: ActionItemRepository) -> None:
    global _repo
    _repo = repo


def _get_repo() -> ActionItemRepository:
    if _repo is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _repo


class CreateActionItemRequest(BaseModel):
    meeting_id: str
    title: str
    description: str | None = None
    assignee: str | None = None
    priority: str = "medium"
    due_date: str | None = None
    reminder_at: str | None = None


class UpdateActionItemRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    assignee: str | None = None
    status: str | None = None
    priority: str | None = None
    due_date: str | None = None
    reminder_at: str | None = None


@router.get("/api/action-items")
async def list_action_items(
    status: str | None = None,
    assignee: str | None = None,
    due_before: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    items = await _get_repo().list_items(
        status=status, assignee=assignee, due_before=due_before, limit=limit, offset=offset
    )
    return {"items": items}


@router.get("/api/action-items/{item_id}")
async def get_action_item(item_id: str):
    item = await _get_repo().get(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="Action item not found")
    return item


@router.post("/api/action-items", status_code=201)
async def create_action_item(body: CreateActionItemRequest):
    repo = _get_repo()
    item_id = await repo.create(
        meeting_id=body.meeting_id,
        title=body.title,
        description=body.description,
        assignee=body.assignee,
        priority=body.priority,
        due_date=body.due_date,
        reminder_at=body.reminder_at,
        source="manual",
    )
    return await repo.get(item_id)


@router.patch("/api/action-items/{item_id}")
async def update_action_item(item_id: str, body: UpdateActionItemRequest):
    repo = _get_repo()
    if not await repo.get(item_id):
        raise HTTPException(status_code=404, detail="Action item not found")
    fields = body.model_dump(exclude_none=True)
    if fields:
        await repo.update(item_id, **fields)
    return await repo.get(item_id)


@router.delete("/api/action-items/{item_id}", status_code=204)
async def delete_action_item(item_id: str):
    if not await _get_repo().get(item_id):
        raise HTTPException(status_code=404, detail="Action item not found")
    await _get_repo().delete(item_id)


@router.get("/api/meetings/{meeting_id}/action-items")
async def get_meeting_action_items(meeting_id: str):
    items = await _get_repo().list_by_meeting(meeting_id)
    return {"items": items}
