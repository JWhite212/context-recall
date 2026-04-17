"""
Template management endpoints.

GET  /api/templates              - list all templates
GET  /api/templates/{name}       - get a specific template
POST /api/templates              - create/update a custom template
DELETE /api/templates/{name}     - delete a custom template
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.templates import SummaryTemplate, TemplateManager

logger = logging.getLogger("meetingmind.api.templates")

router = APIRouter()


class TemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    system_prompt: str = Field(max_length=50000)
    sections: list[str] = Field(default=[], max_length=50)


class TemplateResponse(BaseModel):
    name: str
    description: str
    system_prompt: str
    sections: list[str]


@router.get("/api/templates", response_model=list[TemplateResponse])
async def list_templates():
    tm = TemplateManager()
    return [
        TemplateResponse(
            name=t.name,
            description=t.description,
            system_prompt=t.system_prompt,
            sections=t.sections,
        )
        for t in tm.list_templates()
    ]


@router.get("/api/templates/{name}", response_model=TemplateResponse)
async def get_template(name: str):
    tm = TemplateManager()
    template = tm.get_template(name)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
    return TemplateResponse(
        name=template.name,
        description=template.description,
        system_prompt=template.system_prompt,
        sections=template.sections,
    )


@router.post("/api/templates", response_model=TemplateResponse, status_code=201)
async def save_template(body: TemplateRequest):
    tm = TemplateManager()
    template = SummaryTemplate(
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        sections=body.sections,
    )
    tm.save_template(template)
    return TemplateResponse(
        name=template.name,
        description=template.description,
        system_prompt=template.system_prompt,
        sections=template.sections,
    )


@router.delete("/api/templates/{name}")
async def delete_template(name: str):
    tm = TemplateManager()
    deleted = tm.delete_template(name)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{name}' not found or is a built-in template",
        )
    return {"deleted": True, "name": name}
