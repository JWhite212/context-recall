"""
FastAPI server for MeetingMind.

Runs on a background thread alongside the existing detector loop,
providing a REST + WebSocket interface for the UI.
"""

import asyncio
import logging
import threading

import uvicorn
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from src.api.auth import verify_token
from src.api.events import EventBus
from src.api.routes import config as config_routes
from src.api.routes import devices as devices_routes
from src.api.routes import export as export_routes
from src.api.routes import resummarise as resummarise_routes
from src.api.routes import meetings as meetings_routes
from src.api.routes import models as models_routes
from src.api.routes import recording as recording_routes
from src.api.routes import status as status_routes
from src.utils.config import DEFAULT_CONFIG_PATH, load_config
from src.api.websocket import ConnectionManager
from src.db.database import Database
from src.db.repository import MeetingRepository

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
        recording_routes.init(
            self._start_recording, self._stop_recording, self._is_recording
        )

        export_routes.init(self.repo)
        resummarise_routes.init(self.repo)

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

        # WebSocket endpoint (no auth — the UI running on localhost is trusted).
        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await self.ws_manager.connect(websocket)
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self.ws_manager.disconnect(websocket)

        return app

    async def _run_async(self) -> None:
        """Async entry point for the server thread."""
        self._loop = asyncio.get_event_loop()
        self.event_bus.set_loop(self._loop)

        # Wire EventBus → WebSocket broadcast.
        self.event_bus.subscribe_async(self.ws_manager.broadcast)

        # Connect database.
        await self.db.connect()
        self.repo = MeetingRepository(self.db)

        # Re-init routes now that repo is ready.
        meetings_routes.init(self.repo)
        export_routes.init(self.repo)
        resummarise_routes.init(self.repo)

        # Run data retention cleanup on startup.
        try:
            config = load_config()
            r = config.retention
            if r.audio_retention_days > 0 or r.record_retention_days > 0:
                result = await self.repo.cleanup_old_meetings(
                    r.audio_retention_days, r.record_retention_days
                )
                logger.info("Retention cleanup: %s", result)
        except Exception as e:
            logger.warning("Retention cleanup failed: %s", e)

        self._app = self._create_app()

        config = uvicorn.Config(
            app=self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        logger.info("API server starting on http://%s:%d", self.host, self.port)
        await self._server.serve()

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
        """Signal the server to shut down."""
        if self._server:
            self._server.should_exit = True
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("API server stopped")

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop
