"""
Audio device listing endpoint.

GET /api/devices — returns available audio input devices.
"""

import sounddevice as sd
from fastapi import APIRouter

from src.api.schemas import DeviceListResponse

router = APIRouter()


@router.get("/api/devices", response_model=DeviceListResponse, summary="List audio devices")
async def list_devices():
    devices = sd.query_devices()
    inputs = []
    for i, dev in enumerate(devices):
        if dev["max_input_channels"] > 0:
            inputs.append({
                "index": i,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "sample_rate": dev["default_samplerate"],
                "is_default": i == sd.default.device[0],
            })
    return {"devices": inputs}
