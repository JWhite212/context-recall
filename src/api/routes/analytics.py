"""API routes for meeting analytics."""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from src.analytics.engine import AnalyticsEngine

router = APIRouter(prefix="/api/analytics", tags=["analytics"])
_engine: AnalyticsEngine | None = None


def init(engine: AnalyticsEngine) -> None:
    global _engine
    _engine = engine


def _get_engine() -> AnalyticsEngine:
    if _engine is None:
        raise HTTPException(status_code=503, detail="Service not initialized")
    return _engine


@router.get("/summary")
async def get_summary(period: str = "weekly"):
    now = datetime.now(timezone.utc)
    if period == "daily":
        period_start = now.strftime("%Y-%m-%d")
    elif period == "weekly":
        period_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    else:
        period_start = now.strftime("%Y-%m-01")
    current = await _get_engine().get_period_summary(period, period_start)
    return {"current_period": current, "period_type": period, "period_start": period_start}


@router.get("/trends")
async def get_trends(period_type: str = "weekly", weeks: int = 8):
    now = datetime.now(timezone.utc)
    start = (now - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    data = await _get_engine().get_period_range(period_type, start, end)
    return {"trends": data, "period_type": period_type}


@router.get("/people")
async def get_people(limit: int = 10):
    return {"people": await _get_engine().get_most_met_people(limit=limit)}


@router.get("/health")
async def get_health():
    engine = _get_engine()
    load_score = await engine.compute_load_score()
    indicators = await engine.get_health_indicators()
    return {"load_score": load_score, "indicators": indicators}


@router.post("/refresh", status_code=202)
async def refresh_analytics():
    await _get_engine().refresh_current_periods()
    return {"status": "refreshed"}
