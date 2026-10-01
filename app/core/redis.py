from fastapi import Request
from redis.asyncio import Redis

from app.core.config import settings


def create_redis() -> Redis:
    return Redis.from_url(settings.REDIS_URL, decode_responses=True)


def get_redis(request: Request) -> Redis:
    """FastAPI dependency: the Redis client created in the app lifespan."""
    redis: Redis = request.app.state.redis
    return redis
