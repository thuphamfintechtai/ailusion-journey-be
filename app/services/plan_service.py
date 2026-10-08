"""Trip plans: the form goes to the LLM service, which returns 3 options and a decision.

The plan itself lives in the LLM service (its checkpoint); like chat, the only thing we keep
is which plans belong to which user — a Redis sorted set per user:

    plan:threads:<user_id>  ->  {thread_id: unix_ts}

Kept apart from `chat:threads:*` so plans do not show up in the chat thread list.

The thread id may come from the client (`plan-` + 16 hex) so the browser can open the
progress stream before the plan returns. The id is claimed for the user *before* calling
upstream — the progress endpoint checks ownership while the plan is still running — and
released again if the plan fails.
"""

import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.schemas.plan import PlanCreate
from app.services import llm_client


async def create_plan(
    *, redis: Redis, client: httpx.AsyncClient, user_id: uuid.UUID, data: PlanCreate
) -> dict[str, Any]:
    thread_id = data.thread_id or new_thread_id()
    await _claim(redis, user_id, thread_id)
    try:
        return await llm_client.create_plan(
            client, form=data.form(), thread_id=thread_id, user_id=str(user_id)
        )
    except Exception:
        await redis.zrem(_index_key(user_id), thread_id)
        raise


async def ensure_owned(*, redis: Redis, user_id: uuid.UUID, thread_id: str) -> None:
    await _ensure_owned(redis, user_id, thread_id)


@asynccontextmanager
async def stream_events(
    *,
    redis: Redis,
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    thread_id: str,
    last_event_id: str | None,
) -> AsyncIterator[httpx.Response]:
    await _ensure_owned(redis, user_id, thread_id)
    async with llm_client.stream_events(
        client, thread_id=thread_id, last_event_id=last_event_id
    ) as upstream:
        yield upstream


def new_thread_id() -> str:
    return f"plan-{secrets.token_hex(8)}"


def _index_key(user_id: uuid.UUID) -> str:
    return f"plan:threads:{user_id}"


async def _claim(redis: Redis, user_id: uuid.UUID, thread_id: str) -> None:
    key = _index_key(user_id)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zadd(key, {thread_id: time.time()})
        pipe.expire(key, settings.PLAN_THREAD_TTL_MINUTES * 60)
        await pipe.execute()


async def _ensure_owned(redis: Redis, user_id: uuid.UUID, thread_id: str) -> None:
    """404 rather than 403: someone else's plan id should look like it does not exist."""
    if await redis.zscore(_index_key(user_id), thread_id) is None:
        raise NotFoundError("Plan not found")
