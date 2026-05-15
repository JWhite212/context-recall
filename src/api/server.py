"""
FastAPI server for Context Recall.

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
from src.api.routes import calendar as calendar_routes
from src.api.routes import config as config_routes
from src.api.routes import devices as devices_routes
from src.api.routes import diagnostics as diagnostics_routes
from src.api.routes import export as export_routes
from src.api.routes import meetings as meetings_routes
from src.api.routes import models as models_routes
from src.api.routes import preflight as preflight_routes
from src.api.routes import recording as recording_routes
from src.api.routes import reprocess as reprocess_routes
from src.api.routes import resummarise as resummarise_routes
from src.api.routes import search as search_routes
from src.api.routes import speakers as speakers_routes
from src.api.routes import status as status_routes
from src.api.routes import support_bundle as support_bundle_routes
from src.api.routes import templates as templates_routes
from src.api.websocket import ConnectionManager
from src.db.database import Database
from src.db.repository import MeetingRepository
from src.embeddings import Embedder, is_embeddings_available
from src.utils.config import DEFAULT_CONFIG_PATH, load_config

logger = logging.getLogger("contextrecall.api")


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

        # State accessors (set by Context Recall before start).
        self._get_daemon_state = lambda: "idle"
        self._get_active_meeting = lambda: None

        # Recording controls (set by Context Recall before start).
        self._start_recording = None
        self._stop_recording = None
        self._stop_recording_deferred = None
        self._is_recording = None

    def set_state_accessors(self, get_daemon_state, get_active_meeting) -> None:
        self._get_daemon_state = get_daemon_state
        self._get_active_meeting = get_active_meeting

    def set_recording_controls(self, start, stop, stop_deferred, is_recording) -> None:
        self._start_recording = start
        self._stop_recording = stop
        self._stop_recording_deferred = stop_deferred
        self._is_recording = is_recording

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="Context Recall API",
            description=(
                "REST + WebSocket API for the Context Recall daemon. "
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
        recording_routes.init(
            self._start_recording,
            self._stop_recording,
            self._stop_recording_deferred,
            self._is_recording,
        )

        calendar_routes.init(self.repo)
        export_routes.init(self.repo)
        resummarise_routes.init(self.repo, self.event_bus)
        reprocess_routes.init(self.repo, self.event_bus)
        models_routes.init(self.event_bus)

        # Initialise embedder for semantic search (if available).
        embedder = None
        if is_embeddings_available():
            try:
                embedder = Embedder()
            except Exception as e:
                logger.warning("Failed to initialise embedder: %s", e)

        search_routes.init(self.repo, embedder)
        speakers_routes.init(self.repo)

        # Register REST routers with auth dependency.
        auth_deps = [Depends(verify_token)]
        app.include_router(status_routes.router, dependencies=auth_deps)
        app.include_router(meetings_routes.router, dependencies=auth_deps)
        app.include_router(config_routes.router, dependencies=auth_deps)
        app.include_router(recording_routes.router, dependencies=auth_deps)
        app.include_router(devices_routes.router, dependencies=auth_deps)
        app.include_router(preflight_routes.router, dependencies=auth_deps)
        app.include_router(diagnostics_routes.router, dependencies=auth_deps)
        app.include_router(support_bundle_routes.router, dependencies=auth_deps)
        app.include_router(export_routes.router, dependencies=auth_deps)
        app.include_router(resummarise_routes.router, dependencies=auth_deps)
        app.include_router(reprocess_routes.router, dependencies=auth_deps)
        app.include_router(models_routes.router, dependencies=auth_deps)
        app.include_router(templates_routes.router, dependencies=auth_deps)
        app.include_router(search_routes.router, dependencies=auth_deps)
        app.include_router(speakers_routes.router, dependencies=auth_deps)
        app.include_router(calendar_routes.router, dependencies=auth_deps)

        # Intelligence feature routes.
        from src.action_items.repository import ActionItemRepository
        from src.analytics.engine import AnalyticsEngine
        from src.analytics.repository import AnalyticsRepository
        from src.api.routes import action_items as action_items_routes
        from src.api.routes import analytics as analytics_routes
        from src.api.routes import notifications as notifications_routes
        from src.api.routes import prep as prep_routes
        from src.api.routes import series as series_routes
        from src.notifications.repository import NotificationRepository
        from src.prep.repository import PrepRepository
        from src.series.repository import SeriesRepository

        ai_repo = ActionItemRepository(self.db)
        series_repo = SeriesRepository(self.db)
        analytics_repo = AnalyticsRepository(self.db)
        notif_repo = NotificationRepository(self.db)
        prep_repo = PrepRepository(self.db)

        analytics_engine = AnalyticsEngine(
            config=load_config().analytics,
            meeting_repo=self.repo,
            analytics_repo=analytics_repo,
            action_item_repo=ai_repo,
        )

        action_items_routes.init(ai_repo)
        series_routes.init(series_repo)
        analytics_routes.init(analytics_engine)
        notifications_routes.init(notif_repo)
        prep_routes.init(prep_repo)

        app.include_router(action_items_routes.router, dependencies=auth_deps)
        app.include_router(series_routes.router, dependencies=auth_deps)
        app.include_router(analytics_routes.router, dependencies=auth_deps)
        app.include_router(notifications_routes.router, dependencies=auth_deps)
        app.include_router(prep_routes.router, dependencies=auth_deps)

        # WebSocket endpoint with message-based auth handshake.
        # The client connects, then sends {"type":"auth","token":"<value>"}
        # as its first message. Legacy query-param auth also accepted.
        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            await websocket.accept()

            # Check for legacy query-param token first.
            legacy_token = websocket.query_params.get("token", "")
            if legacy_token and hmac.compare_digest(legacy_token, _get_token()):
                authenticated = True
            else:
                # Wait for auth message within 5 seconds.
                authenticated = False
                try:
                    import json as _json

                    msg = await asyncio.wait_for(
                        websocket.receive_text(),
                        timeout=5.0,
                    )
                    data = _json.loads(msg)
                    if (
                        isinstance(data, dict)
                        and data.get("type") == "auth"
                        and isinstance(data.get("token"), str)
                        and hmac.compare_digest(data["token"], _get_token())
                    ):
                        authenticated = True
                except (asyncio.TimeoutError, Exception):
                    pass

            if not authenticated:
                await websocket.close(code=4001, reason="Unauthorized")
                return

            self.ws_manager.add(websocket)
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

        # Recover meetings orphaned by a previous daemon process that died
        # mid-pipeline (Bug C2). Without this, rows stay in 'transcribing'
        # forever and the UI offers no recovery action for them.
        try:
            flipped = await self.repo.reset_stale_inflight_meetings()
            if flipped:
                logger.info("Recovered %d meeting(s) stuck in transient status", flipped)
        except Exception:
            logger.warning("Stale-status recovery failed", exc_info=True)

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

        # Start the intelligence scheduler after app creation.
        from src.scheduler import Scheduler

        self._scheduler = Scheduler()
        self._setup_scheduler_jobs()
        self._scheduler.start()

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
            if hasattr(self, "_scheduler"):
                self._scheduler.stop()
            if self._retention_task and not self._retention_task.done():
                self._retention_task.cancel()
                try:
                    await self._retention_task
                except asyncio.CancelledError:
                    pass
            await self.db.close()

    def _setup_scheduler_jobs(self) -> None:
        """Register background scheduler jobs."""
        config = load_config()

        if config.notifications.enabled:
            self._scheduler.register("reminder_check", self._check_reminders, 60)

        self._scheduler.register(
            "analytics_refresh",
            self._refresh_analytics_periodic,
            config.analytics.refresh_interval_hours * 3600,
        )

        if config.series.heuristic_enabled:
            self._scheduler.register("series_detect", self._run_series_detection, 86400)

    async def _check_reminders(self) -> None:
        """Check for due reminders and overdue action items."""
        from src.action_items.repository import ActionItemRepository
        from src.notifications.dispatcher import NotificationDispatcher
        from src.notifications.repository import NotificationRepository

        config = load_config()
        ai_repo = ActionItemRepository(self.db)
        notif_repo = NotificationRepository(self.db)
        dispatcher = NotificationDispatcher(
            config=config.notifications, repo=notif_repo, event_bus=self.event_bus
        )
        overdue = await ai_repo.list_overdue()
        for item in overdue:
            await dispatcher.notify(
                type="overdue",
                title=f"Overdue: {item['title']}",
                body=f"Assigned to {item.get('assignee', 'unassigned')}. Due: {item['due_date']}",
                reference_id=item["id"],
                priority="high",
            )
        reminders = await ai_repo.list_due_reminders()
        for item in reminders:
            await dispatcher.notify(
                type="reminder",
                title=f"Reminder: {item['title']}",
                body=f"Due: {item.get('due_date', 'not set')}",
                reference_id=item["id"],
                priority="normal",
            )

    async def _refresh_analytics_periodic(self) -> None:
        """Periodic analytics refresh."""
        try:
            from src.action_items.repository import ActionItemRepository
            from src.analytics.engine import AnalyticsEngine
            from src.analytics.repository import AnalyticsRepository

            config = load_config()
            analytics_repo = AnalyticsRepository(self.db)
            ai_repo = ActionItemRepository(self.db)
            engine = AnalyticsEngine(
                config=config.analytics,
                meeting_repo=self.repo,
                analytics_repo=analytics_repo,
                action_item_repo=ai_repo,
            )
            await engine.refresh_current_periods()
        except Exception:
            logger.exception("Periodic analytics refresh failed")

    async def _run_series_detection(self) -> None:
        """Run heuristic series detection."""
        try:
            from src.series.detector import HeuristicSeriesDetector
            from src.series.repository import SeriesRepository

            config = load_config()
            series_repo = SeriesRepository(self.db)
            detector = HeuristicSeriesDetector(
                config=config.series,
                meeting_repo=self.repo,
                series_repo=series_repo,
            )
            new_series = await detector.detect()
            if new_series:
                logger.info("Heuristic detection found %d new series", len(new_series))
        except Exception:
            logger.exception("Heuristic series detection failed")

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
            name="contextrecall-api",
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
