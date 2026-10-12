import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, DbSession, LlmClient, RedisClient, SessionFactory
from app.core.exceptions import AppError
from app.schemas.chat import (
    ChatHistory,
    ChatPlanCreate,
    ChatPlanReply,
    ChatRename,
    ChatReply,
    ChatSend,
    ChatThread,
    TripInfo,
    TripState,
)
from app.schemas.common import Page
from app.services import chat_service

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("", response_model=ChatReply)
async def send_message(
    data: ChatSend, user: CurrentUser, db: DbSession, llm: LlmClient
) -> ChatReply:
    """Ask the travel-planning agent and wait for the full answer."""
    return await chat_service.send_message(
        db=db,
        client=llm,
        user_id=user.id,
        message=data.message,
        thread_id=data.thread_id,
    )


@router.post("/stream")
async def stream_message(
    data: ChatSend,
    user: CurrentUser,
    db: DbSession,
    sessions: SessionFactory,
    llm: LlmClient,
) -> StreamingResponse:
    """Same as `POST /chat` but streams the answer as server-sent events.

    Events: `{"type": "start" | "delta" | "trip" | "done" | "error", ...}`. `trip` carries
    the trip form after this turn (`TripState`); `open_form: true` means "open the form now".
    The question is saved before streaming starts and the answer once it ends.
    """
    user_id = user.id

    async def forward() -> AsyncIterator[bytes]:
        # Headers are already sent once streaming starts, so an error (LLM service down,
        # thread not found…) can no longer become an HTTP status: send it as an SSE `error`
        # event the client already handles, instead of cutting the stream.
        try:
            async with chat_service.stream_message(
                db=db,
                session_factory=sessions,
                client=llm,
                user_id=user_id,
                message=data.message,
                thread_id=data.thread_id,
            ) as chunks:
                async for chunk in chunks:
                    yield chunk
        except AppError as exc:
            event = {"type": "error", "message": exc.message}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        forward(),
        media_type="text/event-stream",
        # Keep proxies from buffering the stream into one big response.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/threads", response_model=Page[ChatThread])
async def list_threads(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Tìm trong tiêu đề và nội dung tin nhắn"),
    ] = None,
) -> Page[ChatThread]:
    """The current user's conversations, most recently used first."""
    threads, total = await chat_service.list_threads(
        db=db, user_id=user.id, limit=limit, offset=offset, query=q
    )
    return Page(items=threads, total=total, limit=limit, offset=offset)


@router.get("/threads/{thread_id}", response_model=ChatHistory)
async def get_history(thread_id: str, user: CurrentUser, db: DbSession) -> ChatHistory:
    return await chat_service.get_history(db=db, user_id=user.id, thread_id=thread_id)


@router.get("/threads/{thread_id}/trip-info", response_model=TripState)
async def trip_info(thread_id: str, user: CurrentUser, db: DbSession, llm: LlmClient) -> TripState:
    """Trip form filled from the conversation so far, and the required fields still missing."""
    return await chat_service.trip_info(db=db, client=llm, user_id=user.id, thread_id=thread_id)


@router.put("/threads/{thread_id}/trip-info", response_model=TripState)
async def confirm_trip(
    thread_id: str, data: TripInfo, user: CurrentUser, db: DbSession, llm: LlmClient
) -> TripState:
    """The user confirmed the form (pressed "Lập lộ trình"): later turns use exactly this."""
    return await chat_service.confirm_trip(
        db=db, client=llm, user_id=user.id, thread_id=thread_id, info=data
    )


@router.post("/plan", response_model=ChatPlanReply)
async def plan_in_new_chat(
    data: ChatPlanCreate,
    user: CurrentUser,
    db: DbSession,
    redis: RedisClient,
    llm: LlmClient,
) -> ChatPlanReply:
    """ "Lập lộ trình" from an empty chat: start a conversation, then plan as below.

    The new conversation's id is `thread_id` in the reply.
    """
    return await chat_service.plan_in_chat(
        db=db, redis=redis, client=llm, user_id=user.id, thread_id=None, data=data
    )


@router.post("/threads/{thread_id}/plan", response_model=ChatPlanReply)
async def plan_in_chat(
    thread_id: str,
    data: ChatPlanCreate,
    user: CurrentUser,
    db: DbSession,
    redis: RedisClient,
    llm: LlmClient,
) -> ChatPlanReply:
    """ "Lập lộ trình" in the chat's trip form: build the plan and keep it in the conversation.

    Takes 20-60 s. Send `form.thread_id` and open `GET /plan/{form.thread_id}/events`
    alongside to show progress. The plan and the confirmed form are saved as two messages.
    """
    return await chat_service.plan_in_chat(
        db=db, redis=redis, client=llm, user_id=user.id, thread_id=thread_id, data=data
    )


@router.patch("/threads/{thread_id}", response_model=ChatThread)
async def rename_thread(
    thread_id: str, data: ChatRename, user: CurrentUser, db: DbSession
) -> ChatThread:
    return await chat_service.rename_thread(
        db=db, user_id=user.id, thread_id=thread_id, title=data.title
    )


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(thread_id: str, user: CurrentUser, db: DbSession) -> None:
    await chat_service.delete_thread(db=db, user_id=user.id, thread_id=thread_id)
