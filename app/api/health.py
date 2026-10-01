import logging

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.api.deps import DbSession, RedisClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: str
    checks: dict[str, str] = {}


@router.get("/health", response_model=HealthStatus)
async def liveness() -> HealthStatus:
    """Process is up. Does not touch dependencies."""
    return HealthStatus(status="ok")


@router.get("/health/ready", response_model=HealthStatus)
async def readiness(db: DbSession, redis: RedisClient, response: Response) -> HealthStatus:
    """Ready to serve traffic: PostgreSQL and Redis are reachable."""
    checks: dict[str, str] = {}

    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        logger.exception("Database health check failed")
        checks["database"] = "error"

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except Exception:
        logger.exception("Redis health check failed")
        checks["redis"] = "error"

    healthy = all(value == "ok" for value in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthStatus(status="ok" if healthy else "error", checks=checks)
