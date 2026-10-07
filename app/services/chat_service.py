"""Chat orchestration: who owns which conversation, then delegate to the LLM service.

The conversation itself (messages, agent memory) lives in the LLM service, keyed by
`thread_id`. What we keep here is only the index "which threads belong to which user",
in a Redis sorted set scored by last activity:

    chat:threads:<user_id>  ->  { thread_id: <unix timestamp> }

That gives us two things: listing a user's conversations newest-first, and refusing a
`thread_id` that belongs to someone else. The index expires on the same horizon as the
LLM service's checkpoints, so it does not accumulate dead threads.
"""

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import httpx
from redis.asyncio import Redis

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.schemas.chat import ChatHistory, ChatMessage, ChatReply, ChatThread
from app.services import llm_client


async def send_message(
    *,
    redis: Redis,
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    message: str,
    thread_id: str | None,
) -> ChatReply:
    """Send one turn and return the assistant's answer."""
    await _ensure_owned(redis, user_id, thread_id)

    body = await llm_client.send_message(
        client, message=message, thread_id=thread_id, user_id=str(user_id)
    )
    reply = ChatReply.model_validate(body)
    await _touch(redis, user_id, reply.thread_id)
    return reply


@asynccontextmanager
async def stream_message(
    *,
    redis: Redis,
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    message: str,
    thread_id: str | None,
) -> AsyncIterator[httpx.Response]:
    """Same as `send_message` but yields the upstream SSE response to forward."""
    await _ensure_owned(redis, user_id, thread_id)

    # A brand-new thread gets its id here (not upstream) so we can record ownership
    # before streaming starts — once bytes are flowing we can no longer fail cleanly.
    resolved = thread_id or _new_thread_id()
    await _touch(redis, user_id, resolved)

    async with llm_client.stream_message(
        client, message=message, thread_id=resolved, user_id=str(user_id)
    ) as response:
        yield response


async def get_history(
    *, redis: Redis, client: httpx.AsyncClient, user_id: uuid.UUID, thread_id: str
) -> ChatHistory:
    await _ensure_owned(redis, user_id, thread_id)

    body = await llm_client.get_history(client, thread_id)
    return ChatHistory(
        thread_id=thread_id,
        messages=[ChatMessage.model_validate(m) for m in body.get("messages", [])],
    )


async def list_threads(
    *, redis: Redis, user_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[ChatThread], int]:
    key = _index_key(user_id)
    total = await redis.zcard(key)
    # withscores=True -> [(thread_id, unix_timestamp), ...], highest score first.
    # redis-py types this as a broad union; decode_responses=True makes it str keys.
    rows = cast(
        "list[tuple[str, float]]",
        await redis.zrevrange(key, offset, offset + limit - 1, withscores=True),
    )
    threads = [
        ChatThread(thread_id=thread_id, last_active_at=datetime.fromtimestamp(score, tz=UTC))
        for thread_id, score in rows
    ]
    return threads, int(total)


async def delete_thread(*, redis: Redis, user_id: uuid.UUID, thread_id: str) -> None:
    """Forget the conversation on our side. Upstream state expires on its own TTL."""
    removed = await redis.zrem(_index_key(user_id), thread_id)
    if not removed:
        raise _thread_not_found()


def _index_key(user_id: uuid.UUID) -> str:
    return f"chat:threads:{user_id}"


def _new_thread_id() -> str:
    return f"chat-{uuid.uuid4().hex[:12]}"


def _thread_not_found() -> NotFoundError:
    return NotFoundError("Conversation not found")


async def _ensure_owned(redis: Redis, user_id: uuid.UUID, thread_id: str | None) -> None:
    """Continuing a conversation requires owning it.

    404 rather than 403: someone else's thread id should look like it does not exist.
    """
    if thread_id is None:
        return
    if await redis.zscore(_index_key(user_id), thread_id) is None:
        raise _thread_not_found()


async def _touch(redis: Redis, user_id: uuid.UUID, thread_id: str) -> None:
    key = _index_key(user_id)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zadd(key, {thread_id: time.time()})
        pipe.expire(key, settings.CHAT_THREAD_TTL_MINUTES * 60)
        await pipe.execute()
