"""
Client / project endpoints.

GET    /api/clients                        — list clients (+ ?include_archived)
POST   /api/clients                        — create a client
PATCH  /api/clients/{id}                   — update
DELETE /api/clients/{id}                   — delete (meetings unassign, projects unlink)
GET    /api/projects                       — list projects (+ ?client_id filter)
POST   /api/projects                       — create a project
PATCH  /api/projects/{id}                  — update
DELETE /api/projects/{id}                  — delete (meetings unassign)
PATCH  /api/meetings/{id}/assignment       — manually assign / clear a meeting
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("contextrecall.api.clients")

router = APIRouter()

_repo = None  # MeetingRepository
_cp_repo = None  # ClientProjectRepository


def init(repo, cp_repo) -> None:
    global _repo, _cp_repo
    _repo = repo
    _cp_repo = cp_repo


def _require_repos() -> None:
    if not _repo or not _cp_repo:
        raise HTTPException(status_code=503, detail="Repository not available")


class ClientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=8000)
    aliases: list[str] = Field(default_factory=list)
    email_domains: list[str] = Field(default_factory=list)


class ClientUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=8000)
    aliases: list[str] | None = None
    email_domains: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    client_id: str | None = None
    description: str = Field(default="", max_length=8000)
    aliases: list[str] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    client_id: str | None = None
    description: str | None = Field(default=None, max_length=8000)
    aliases: list[str] | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class AssignmentUpdate(BaseModel):
    client_id: str | None = None
    project_id: str | None = None


@router.get("/api/clients")
async def list_clients(include_archived: bool = False):
    _require_repos()
    return await _cp_repo.list_clients(include_archived=include_archived)


@router.post("/api/clients", status_code=201)
async def create_client(body: ClientCreate):
    _require_repos()
    client_id = await _cp_repo.create_client(
        name=body.name.strip(),
        description=body.description,
        aliases=[a.strip() for a in body.aliases if a.strip()],
        email_domains=[d.strip() for d in body.email_domains if d.strip()],
    )
    return await _cp_repo.get_client(client_id)


@router.patch("/api/clients/{client_id}")
async def update_client(client_id: str, body: ClientUpdate):
    _require_repos()
    if not await _cp_repo.get_client(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.description is not None:
        fields["description"] = body.description
    if body.aliases is not None:
        fields["aliases_json"] = [a.strip() for a in body.aliases if a.strip()]
    if body.email_domains is not None:
        fields["email_domains_json"] = [d.strip() for d in body.email_domains if d.strip()]
    if body.status is not None:
        fields["status"] = body.status
    if fields:
        await _cp_repo.update_client(client_id, **fields)
    return await _cp_repo.get_client(client_id)


@router.delete("/api/clients/{client_id}")
async def delete_client(client_id: str):
    _require_repos()
    if not await _cp_repo.delete_client(client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    return {"deleted": client_id}


@router.get("/api/projects")
async def list_projects(client_id: str | None = None, include_archived: bool = False):
    _require_repos()
    return await _cp_repo.list_projects(client_id=client_id, include_archived=include_archived)


@router.post("/api/projects", status_code=201)
async def create_project(body: ProjectCreate):
    _require_repos()
    if body.client_id and not await _cp_repo.get_client(body.client_id):
        raise HTTPException(status_code=404, detail="Client not found")
    project_id = await _cp_repo.create_project(
        name=body.name.strip(),
        client_id=body.client_id,
        description=body.description,
        aliases=[a.strip() for a in body.aliases if a.strip()],
    )
    return await _cp_repo.get_project(project_id)


@router.patch("/api/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate):
    _require_repos()
    if not await _cp_repo.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.client_id is not None:
        if body.client_id and not await _cp_repo.get_client(body.client_id):
            raise HTTPException(status_code=404, detail="Client not found")
        fields["client_id"] = body.client_id or None
    if body.description is not None:
        fields["description"] = body.description
    if body.aliases is not None:
        fields["aliases_json"] = [a.strip() for a in body.aliases if a.strip()]
    if body.status is not None:
        fields["status"] = body.status
    if fields:
        await _cp_repo.update_project(project_id, **fields)
    return await _cp_repo.get_project(project_id)


@router.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    _require_repos()
    if not await _cp_repo.delete_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"deleted": project_id}


@router.patch("/api/meetings/{meeting_id}/assignment")
async def set_meeting_assignment(meeting_id: str, body: AssignmentUpdate):
    """Manually assign (or clear) a meeting's client/project.

    Manual assignments carry source='manual' and are never overwritten
    by the automatic passes. Passing nulls clears the assignment (and
    resets the source so auto-assignment may run again on reprocess).
    """
    _require_repos()
    meeting = await _repo.get_meeting(meeting_id)
    if not meeting:
        raise HTTPException(status_code=404, detail="Meeting not found")

    client_id = body.client_id
    project_id = body.project_id
    if project_id:
        project = await _cp_repo.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        if client_id and project.get("client_id") and project["client_id"] != client_id:
            raise HTTPException(
                status_code=422,
                detail="Project belongs to a different client",
            )
        # A project pick implies its client unless one was given.
        if not client_id and project.get("client_id"):
            client_id = project["client_id"]
    if client_id and not await _cp_repo.get_client(client_id):
        raise HTTPException(status_code=404, detail="Client not found")

    cleared = client_id is None and project_id is None
    await _repo.update_meeting(
        meeting_id,
        client_id=client_id,
        project_id=project_id,
        assignment_source="" if cleared else "manual",
        assignment_confidence=0.0 if cleared else 1.0,
    )
    return {
        "meeting_id": meeting_id,
        "client_id": client_id,
        "project_id": project_id,
        "assignment_source": "" if cleared else "manual",
    }
