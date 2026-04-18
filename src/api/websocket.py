"""
WebSocket connection manager for MeetingMind.

Maintains a set of connected WebSocket clients and broadcasts
events from the EventBus to all of them.
"""

import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("meetingmind.websocket")


class ConnectionManager:
    """Manages active WebSocket connections and broadcasts events."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("WebSocket client connected (%d total)", len(self._connections))

    def add(self, websocket: WebSocket) -> None:
        """Register an already-accepted WebSocket connection."""
        self._connections.append(websocket)
        logger.info("WebSocket client connected (%d total)", len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections = [ws for ws in self._connections if ws is not websocket]
        logger.info("WebSocket client disconnected (%d remaining)", len(self._connections))

    async def broadcast(self, event: dict) -> None:
        """Send an event to all connected clients.

        Silently removes clients that have disconnected.
        """
        if not self._connections:
            return

        data = json.dumps(event)
        disconnected: list[WebSocket] = []

        for ws in self._connections:
            try:
                await ws.send_text(data)
            except Exception:
                disconnected.append(ws)

        for ws in disconnected:
            self.disconnect(ws)
