"""
Pydantic response models for the MeetingMind API.

These models provide type documentation and auto-generate OpenAPI schemas
at /docs. They do NOT re-validate outgoing data (use mode="serialization"
to avoid unnecessary conversion overhead).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# ---------- Health / Status ----------


class HealthResponse(BaseModel):
    status: str
    timestamp: float


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    state: str
    timestamp: float
    active_meeting: dict | None = None


# ---------- Meetings ----------


class MeetingResponse(BaseModel):
    """Single meeting record (serialised via Meeting.to_dict())."""
    model_config = ConfigDict(extra="allow")

    id: str
    title: str | None = None
    status: str | None = None
    started_at: float | None = None
    duration_seconds: float | None = None


class MeetingListResponse(BaseModel):
    meetings: list[dict]
    total: int
    limit: int
    offset: int


class DeleteResponse(BaseModel):
    deleted: bool


# ---------- Recording ----------


class RecordStartResponse(BaseModel):
    status: str
    started_at: float


class RecordStopResponse(BaseModel):
    status: str


# ---------- Devices ----------


class AudioDevice(BaseModel):
    index: int
    name: str
    channels: int
    sample_rate: float
    is_default: bool


class DeviceListResponse(BaseModel):
    devices: list[AudioDevice]


# ---------- Models ----------


class ModelInfo(BaseModel):
    name: str
    repo: str
    size_mb: int
    status: str
    percent: int
    error: str | None = None


class ModelListResponse(BaseModel):
    models: list[ModelInfo]


class ModelDownloadResponse(BaseModel):
    status: str
    model: str | None = None


# ---------- Re-summarise ----------


class ResummariseResponse(BaseModel):
    meeting_id: str
    title: str
    tags: list[str]
