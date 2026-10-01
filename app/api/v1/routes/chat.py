from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, LlmClient, RedisClient
from app.schemas.chat import ChatHistory, ChatReply, ChatSend, ChatThread
from app.schemas.common import Page
from app.services import chat_service

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatReply)
async def send_message(
    data: ChatSend, user: CurrentUser, redis: RedisClient, llm: LlmClient
) -> ChatReply:
    """Ask the travel-planning agent and wait for the full answer."""
    return await chat_service.send_message(
        redis=redis,
        client=llm,
        user_id=user.id,
        message=data.message,
        thread_id=data.thread_id,
    )


@router.post("/stream")
async def stream_message(
    data: ChatSend, user: CurrentUser, redis: RedisClient, llm: LlmClient
) -> StreamingResponse:
    """Same as `POST /chat` but streams the answer as server-sent events.

    Events: `{"type": "start" | "delta" | "done" | "error", ...}`.
    """

    async def forward() -> AsyncIterator[bytes]:
        async with chat_service.stream_message(
            redis=redis,
            client=llm,
            user_id=user.id,
            message=data.message,
            thread_id=data.thread_id,
        ) as upstream:
            async for chunk in upstream.aiter_bytes():
                yield chunk

    return StreamingResponse(
        forward(),
        media_type="text/event-stream",
        # Keep proxies from buffering the stream into one big response.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/threads", response_model=Page[ChatThread])
async def list_threads(
    user: CurrentUser,
    redis: RedisClient,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[ChatThread]:
    """The current user's conversations, most recently used first."""
    threads, total = await chat_service.list_threads(
        redis=redis, user_id=user.id, limit=limit, offset=offset
    )
    return Page(items=threads, total=total, limit=limit, offset=offset)


@router.get("/threads/{thread_id}", response_model=ChatHistory)
async def get_history(
    thread_id: str, user: CurrentUser, redis: RedisClient, llm: LlmClient
) -> ChatHistory:
    return await chat_service.get_history(
        redis=redis, client=llm, user_id=user.id, thread_id=thread_id
    )


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: str, user: CurrentUser, redis: RedisClient) -> None:
    await chat_service.delete_thread(redis=redis, user_id=user.id, thread_id=thread_id)
