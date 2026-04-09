"""
Model management endpoints.

GET  /api/models              — list available Whisper models with download status.
POST /api/models/{name}/download — trigger a model download (runs in background thread).
"""

import logging
import threading

from fastapi import APIRouter, HTTPException

logger = logging.getLogger("meetingmind.api.models")

router = APIRouter()

# Track active downloads: model_name → progress dict.
_downloads: dict[str, dict] = {}
_download_lock = threading.Lock()

# Models we expose in the UI (subset of faster-whisper's full list).
AVAILABLE_MODELS = {
    "tiny.en": {"repo": "Systran/faster-whisper-tiny.en", "size_mb": 75},
    "base.en": {"repo": "Systran/faster-whisper-base.en", "size_mb": 145},
    "small.en": {"repo": "Systran/faster-whisper-small.en", "size_mb": 470},
    "medium.en": {"repo": "Systran/faster-whisper-medium.en", "size_mb": 1460},
    "large-v3": {"repo": "Systran/faster-whisper-large-v3", "size_mb": 2950},
}


def _is_downloaded(repo_id: str) -> bool:
    """Check if a model is already in the HuggingFace cache."""
    try:
        from huggingface_hub import scan_cache_dir

        cache = scan_cache_dir()
        return any(repo.repo_id == repo_id for repo in cache.repos)
    except Exception:
        return False


def _download_worker(model_name: str) -> None:
    """Background thread that downloads a model."""
    try:
        from faster_whisper.utils import download_model

        logger.info("Downloading model: %s", model_name)
        download_model(model_name)

        with _download_lock:
            _downloads[model_name] = {"status": "complete", "error": None}

        logger.info("Model download complete: %s", model_name)
    except Exception as e:
        logger.error("Model download failed: %s — %s", model_name, e)
        with _download_lock:
            _downloads[model_name] = {"status": "error", "error": str(e)}


@router.get("/api/models")
async def list_models():
    models = []
    for name, info in AVAILABLE_MODELS.items():
        downloaded = _is_downloaded(info["repo"])

        # Check if there's an active download.
        with _download_lock:
            dl = _downloads.get(name)

        status = "downloaded" if downloaded else "not_downloaded"
        if dl:
            if dl["status"] == "downloading":
                status = "downloading"
            elif dl["status"] == "error" and not downloaded:
                status = "error"

        models.append({
            "name": name,
            "repo": info["repo"],
            "size_mb": info["size_mb"],
            "status": status,
            "error": dl["error"] if dl and dl["status"] == "error" else None,
        })

    return {"models": models}


@router.post("/api/models/{model_name}/download")
async def download_model(model_name: str):
    if model_name not in AVAILABLE_MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_name}")

    info = AVAILABLE_MODELS[model_name]
    if _is_downloaded(info["repo"]):
        return {"status": "already_downloaded"}

    with _download_lock:
        dl = _downloads.get(model_name)
        if dl and dl["status"] == "downloading":
            return {"status": "already_downloading"}
        # Set status under lock before spawning thread to prevent TOCTOU race.
        _downloads[model_name] = {"status": "downloading", "error": None}

    thread = threading.Thread(
        target=_download_worker,
        args=(model_name,),
        name=f"model-download-{model_name}",
        daemon=True,
    )
    thread.start()

    return {"status": "started", "model": model_name}
