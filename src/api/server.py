"""
FastAPI server for MeetingMind.

Runs on a background thread alongside the existing detector loop,
providing a REST + WebSocket interface for the UI.
"""

import asyncio
import hmac
import logging
import threading

import uvicorn
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import _get_token, verify_token
from src.api.events import EventBus
from src.api.routes import config as config_routes
from src.api.routes import devices as devices_routes
from src.api.routes import export as export_routes
from src.api.routes import meetings as meetings_routes
from src.api.routes import models as models_routes
from src.api.routes import recording as recording_routes
from src.api.routes import resummarise as resummarise_routes
from src.api.routes import search as search_routes
from src.api.routes import status as status_routes
from src.api.websocket import ConnectionManager
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.embeddings import Embedder, is_embeddings_available
from src.utils.config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger("meetingmind.api")


class ApiServer:
    """Manages the FastAPI application and its background thread."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9876,
        event_bus: EventBus | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.event_bus = event_bus or EventBus()
        self.ws_manager = ConnectionManager()
        self.db = Database()
        self.repo: MeetingRepository | None = None

        self._app: FastAPI | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: uvicorn.Server | None = None
        self._retention_task: asyncio.Task | None = None

        # State accessors (set by MeetingMind before start).
        self._get_daemon_state = lambda: "idle"
        self._get_active_meeting = lambda: None

        # Recording controls (set by MeetingMind before start).
        self._start_recording = None
        self._stop_recording = None
        self._is_recording = None

    def set_state_accessors(self, get_daemon_state, get_active_meeting) -> None:
        self._get_daemon_state = get_daemon_state
        self._get_active_meeting = get_active_meeting

    def set_recording_controls(self, start, stop, is_recording) -> None:
        self._start_recording = start
        self._stop_recording = stop
        self._is_recording = is_recording

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="MeetingMind API",
            description=(
                "REST + WebSocket API for the MeetingMind daemon. "
                "Provides meeting history, live recording controls, configuration, "
                "model management, and real-time events."
            ),
            version="0.1.0",
            docs_url="/docs",
        )

        # CORS for Tauri dev mode (Vite at localhost:1420) and production.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[
                "http://localhost:1420",
                "http://127.0.0.1:1420",
                "http://localhost:5173",
                "http://127.0.0.1:5173",
                "tauri://localhost",
                "https://tauri.localhost",
            ],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # Initialise route dependencies.
        status_routes.init(self._get_daemon_state, self._get_active_meeting)
        meetings_routes.init(self.repo)
        config_routes.init(DEFAULT_CONFIG_PATH)
        recording_routes.init(self._start_recording, self._stop_recording, self._is_recording)

        export_routes.init(self.repo)
        resummarise_routes.init(self.repo)
        models_routes.init(self.event_bus)

        # Initialise embedder for semantic search (if available).
        embedder = None
        if is_embeddings_available():
            try:
                embedder = Embedder()
            except Exception as e:
                logger.warning("Failed to initialise embedder: %s", e)

        search_routes.init(self.repo, embedder)

        # Register REST routers with auth dependency.
        auth_deps = [Depends(verify_token)]
        app.include_router(status_routes.router, dependencies=auth_deps)
        app.include_router(meetings_routes.router, dependencies=auth_deps)
        app.include_router(config_routes.router, dependencies=auth_deps)
        app.include_router(recording_routes.router, dependencies=auth_deps)
        app.include_router(devices_routes.router, dependencies=auth_deps)
        app.include_router(export_routes.router, dependencies=auth_deps)
        app.include_router(resummarise_routes.router, dependencies=auth_deps)
        app.include_router(models_routes.router, dependencies=auth_deps)
        app.include_router(search_routes.router, dependencies=auth_deps)

        # WebSocket endpoint with token auth via query parameter.
        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            token = websocket.query_params.get("token", "")
            if not hmac.compare_digest(token, _get_token()):
                await websocket.close(code=4001, reason="Unauthorized")
                return
            await self.ws_manager.connect(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                pass
            except Exception:
                logger.exception("WebSocket error")
            finally:
                self.ws_manager.disconnect(websocket)

        return app

    async def _run_async(self) -> None:
        """Async entry point for the server thread."""
        self._loop = asyncio.get_running_loop()
        self.event_bus.set_loop(self._loop)

        # Wire EventBus → WebSocket broadcast.
        self.event_bus.subscribe_async(self.ws_manager.broadcast)

        # Connect database and create repository.
        await self.db.connect()
        self.repo = MeetingRepository(self.db)

        # Run data retention cleanup on startup.
        try:
            app_config = load_config()
            r = app_config.retention
            if r.audio_retention_days > 0 or r.record_retention_days > 0:
                result = await self.repo.cleanup_old_meetings(
                    r.audio_retention_days, r.record_retention_days
                )
                logger.info("Retention cleanup: %s", result)
        except Exception as e:
            logger.warning("Retention cleanup failed: %s", e)

        # Create the app (routes are initialized here with the ready repo).
        self._app = self._create_app()

        # Schedule periodic retention cleanup (every 6 hours).
        self._retention_task = asyncio.create_task(self._periodic_retention_cleanup())

        uvi_config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(uvi_config)
        logger.info("API server starting on http://%s:%d", self.host, self.port)
        try:
            await self._server.serve()
        finally:
            # Clean up background tasks and database on shutdown.
            if self._retention_task and not self._retention_task.done():
                self._retention_task.cancel()
                try:
                    await self._retention_task
                except asyncio.CancelledError:
                    pass
            await self.db.close()

    async def _periodic_retention_cleanup(self) -> None:
        """Run data retention cleanup every 6 hours."""
        interval = 6 * 3600  # 6 hours
        while True:
            await asyncio.sleep(interval)
            try:
                config = load_config()
                r = config.retention
                if r.audio_retention_days > 0 or r.record_retention_days > 0:
                    result = await self.repo.cleanup_old_meetings(
                        r.audio_retention_days, r.record_retention_days
                    )
                    if result["audio_deleted"] or result["records_deleted"]:
                        logger.info("Periodic retention cleanup: %s", result)
            except Exception as e:
                logger.warning("Periodic retention cleanup failed: %s", e)

    def _thread_target(self) -> None:
        """Target for the background thread."""
        asyncio.run(self._run_async())

    def start(self) -> None:
        """Start the API server on a background daemon thread."""
        self._thread = threading.Thread(
            target=self._thread_target,
            name="meetingmind-api",
            daemon=True,
        )
        self._thread.start()
        logger.info("API server thread started")

    def stop(self) -> None:
        """Signal the server to shut down.

        Cleanup (task cancellation, DB close) happens in _run_async's finally block.
        """
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("API server stopped")

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop
