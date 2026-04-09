"""
Model management endpoints.

GET  /api/models              — list available Whisper models with download status.
POST /api/models/{name}/download — trigger a model download (runs in background thread).

Downloads emit ``model.download.progress`` events via the EventBus so the UI
can display a real-time progress bar.
"""

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException

from src.api.schemas import ModelDownloadResponse, ModelListResponse

if TYPE_CHECKING:
    from src.api.events import EventBus

logger = logging.getLogger("meetingmind.api.models")

router = APIRouter()

# Module-level EventBus reference, set via init().
_event_bus: "EventBus | None" = None

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


def init(event_bus: "EventBus | None" = None) -> None:
    """Set the EventBus for download progress events."""
    global _event_bus
    _event_bus = event_bus


def _downloaded_repos() -> set[str]:
    """Return repo IDs present in the HuggingFace cache (single scan)."""
    try:
        from huggingface_hub import scan_cache_dir

        cache = scan_cache_dir()
        return {repo.repo_id for repo in cache.repos}
    except Exception:
        return set()


def _is_downloaded(repo_id: str) -> bool:
    """Check if a single model repo is in the HuggingFace cache."""
    return repo_id in _downloaded_repos()


def _download_worker(model_name: str) -> None:
    """Background thread that downloads a model with progress tracking."""
    try:
        from huggingface_hub import HfApi, scan_cache_dir, snapshot_download

        repo_id = AVAILABLE_MODELS[model_name]["repo"]
        size_mb = AVAILABLE_MODELS[model_name]["size_mb"]

        logger.info("Downloading model: %s (%d MB)", model_name, size_mb)

        api = HfApi()
        model_info = api.model_info(repo_id)
        total_bytes = sum(
            s.size for s in (model_info.siblings or []) if s.size
        )

        last_percent = -1

        def _emit_progress(percent: int) -> None:
            nonlocal last_percent
            if percent == last_percent:
                return
            last_percent = percent
            with _download_lock:
                _downloads[model_name]["percent"] = percent
            if _event_bus:
                _event_bus.emit({
                    "type": "model.download.progress",
                    "model": model_name,
                    "percent": percent,
                })

        _emit_progress(0)

        # Monitor download progress by polling cache directory size.
        download_done = threading.Event()

        def _monitor_progress() -> None:
            while not download_done.is_set():
                if total_bytes > 0:
                    try:
                        cache = scan_cache_dir()
                        for repo in cache.repos:
                            if repo.repo_id == repo_id:
                                pct = min(99, int(repo.size_on_disk * 100 / total_bytes))
                                _emit_progress(pct)
                                break
                    except Exception:
                        pass
                download_done.wait(timeout=2.0)

        monitor = threading.Thread(
            target=_monitor_progress,
            daemon=True,
            name=f"model-progress-{model_name}",
        )
        monitor.start()

        try:
            snapshot_download(repo_id)
        finally:
            download_done.set()
            monitor.join(timeout=3)

        with _download_lock:
            _downloads[model_name] = {
                "status": "complete",
                "error": None,
                "percent": 100,
            }

        if _event_bus:
            _event_bus.emit({
                "type": "model.download.progress",
                "model": model_name,
                "percent": 100,
            })

        logger.info("Model download complete: %s", model_name)
    except Exception as e:
        logger.error("Model download failed: %s — %s", model_name, e)
        with _download_lock:
            _downloads[model_name] = {
                "status": "error",
                "error": str(e),
                "percent": 0,
            }
        if _event_bus:
            _event_bus.emit({
                "type": "model.download.progress",
                "model": model_name,
                "percent": 0,
                "error": str(e),
            })


@router.get("/api/models", response_model=ModelListResponse, summary="List Whisper models")
async def list_models():
    # Run the blocking cache scan in a thread pool to keep the event loop free.
    cached_repos = await asyncio.get_running_loop().run_in_executor(
        None, _downloaded_repos
    )
    models = []
    for name, info in AVAILABLE_MODELS.items():
        downloaded = info["repo"] in cached_repos

        with _download_lock:
            dl = _downloads.get(name)

        status = "downloaded" if downloaded else "not_downloaded"
        percent = 0
        if dl:
            if dl["status"] == "downloading":
                status = "downloading"
                percent = dl.get("percent", 0)
            elif dl["status"] == "error" and not downloaded:
                status = "error"
            elif dl["status"] == "complete":
                status = "downloaded"

        models.append({
            "name": name,
            "repo": info["repo"],
            "size_mb": info["size_mb"],
            "status": status,
            "percent": percent,
            "error": dl["error"] if dl and dl["status"] == "error" else None,
        })

    return {"models": models}


@router.post(
    "/api/models/{model_name}/download",
    response_model=ModelDownloadResponse,
    summary="Download a model",
)
async def download_model(model_name: str):
    if model_name not in AVAILABLE_MODELS:
        raise HTTPException(status_code=404, detail=f"Unknown model: {model_name}")

    info = AVAILABLE_MODELS[model_name]
    downloaded = await asyncio.get_running_loop().run_in_executor(
        None, _is_downloaded, info["repo"]
    )
    if downloaded:
        return {"status": "already_downloaded"}

    with _download_lock:
        dl = _downloads.get(model_name)
        if dl and dl["status"] == "downloading":
            return {"status": "already_downloading"}
        _downloads[model_name] = {
            "status": "downloading",
            "error": None,
            "percent": 0,
        }

    thread = threading.Thread(
        target=_download_worker,
        args=(model_name,),
        name=f"model-download-{model_name}",
        daemon=True,
    )
    thread.start()

    return {"status": "started", "model": model_name}
