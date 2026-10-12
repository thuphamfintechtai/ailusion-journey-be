"""Chat orchestration: who owns which conversation, what was said, and the LLM call.

Two stores, two jobs:

- PostgreSQL (`conversations`, `messages`) is the durable record — the thread list,
  titles and every message. It never expires.
- The LLM service keeps the agent's short-term memory per `thread_id` (Redis
  checkpoints with a TTL). When that memory has expired, the recent messages we hold are
  sent along so the agent can pick the conversation up again.

A streamed answer is relayed byte for byte and saved once it ends — including the part
already sent if the client disconnects halfway.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import NotFoundError
from app.models.chat import Conversation, Message
from app.schemas.chat import (
    ChatHistory,
    ChatMessage,
    ChatPlanCreate,
    ChatPlanReply,
    ChatReply,
    ChatThread,
    MissingField,
    TripInfo,
    TripState,
)
from app.services import llm_client, plan_service

logger = logging.getLogger(__name__)

# How many past messages ride along to the LLM service, to rebuild an expired memory.
CONTEXT_MESSAGES = 20
# How many recent messages are read to prefill the trip form.
TRIP_INFO_MESSAGES = 40
TITLE_MAX_CHARS = 80


async def send_message(
    *,
    db: AsyncSession,
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    message: str,
    thread_id: str | None,
) -> ChatReply:
    """Send one turn and return the assistant's answer.

    A new conversation is only saved once the LLM service has answered, so a failed first
    turn leaves nothing behind.
    """
    conversation = await _owned(db, user_id, thread_id) if thread_id else None
    resolved = thread_id or _new_thread_id()
    history = await _recent_messages(db, conversation)

    body = await llm_client.send_message(
        client, message=message, thread_id=resolved, user_id=str(user_id), history=history
    )
    reply = ChatReply.model_validate(body)

    if conversation is None:
        conversation = _new_conversation(reply.thread_id, user_id, message)
        db.add(conversation)
    _append(db, conversation, "human", message)
    _append(db, conversation, "ai", reply.reply)
    await db.commit()
    return reply


@asynccontextmanager
async def stream_message(
    *,
    db: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    message: str,
    thread_id: str | None,
) -> AsyncIterator[AsyncIterator[bytes]]:
    """Open the upstream stream and yield the bytes to forward to the client.

    The conversation and the question are saved before streaming starts — once bytes are
    flowing we can no longer fail cleanly, and an unanswered question should still show up
    in the history. The answer is saved when the stream ends.
    """
    conversation = await _owned(db, user_id, thread_id) if thread_id else None
    history = await _recent_messages(db, conversation)
    if conversation is None:
        conversation = _new_conversation(_new_thread_id(), user_id, message)
        db.add(conversation)
    if history and history[-1] == {"role": "human", "content": message}:
        # Retrying a question that never got an answer: it is already saved, and the
        # LLM service is about to receive it as the new message.
        history.pop()
        conversation.last_active_at = datetime.now(UTC)
    else:
        _append(db, conversation, "human", message)
    await db.commit()
    conversation_id = conversation.id

    async with llm_client.stream_message(
        client,
        message=message,
        thread_id=conversation_id,
        user_id=str(user_id),
        history=history,
    ) as response:
        yield _relay_and_save(response, session_factory, conversation_id)


async def list_threads(
    *,
    db: AsyncSession,
    user_id: uuid.UUID,
    limit: int,
    offset: int,
    query: str | None = None,
) -> tuple[list[ChatThread], int]:
    """The user's conversations, most recently used first.

    `query` matches the title or any message in the conversation (case-insensitive).
    """
    conditions = [Conversation.user_id == user_id]
    if query and query.strip():
        pattern = f"%{_escape_like(query.strip())}%"
        conditions.append(
            or_(
                Conversation.title.ilike(pattern, escape="\\"),
                exists().where(
                    Message.conversation_id == Conversation.id,
                    Message.content.ilike(pattern, escape="\\"),
                ),
            )
        )

    total = await db.scalar(select(func.count()).select_from(Conversation).where(*conditions))
    rows = await db.scalars(
        select(Conversation)
        .where(*conditions)
        .order_by(Conversation.last_active_at.desc(), Conversation.id)
        .limit(limit)
        .offset(offset)
    )
    return [_thread(c) for c in rows], int(total or 0)


async def get_history(*, db: AsyncSession, user_id: uuid.UUID, thread_id: str) -> ChatHistory:
    conversation = await _owned(db, user_id, thread_id)
    rows = await db.scalars(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id)
    )
    return ChatHistory(
        thread_id=conversation.id,
        messages=[
            ChatMessage(role=m.role, content=m.content, plan=m.plan, created_at=m.created_at)
            for m in rows
        ],
    )


async def trip_info(
    *, db: AsyncSession, client: httpx.AsyncClient, user_id: uuid.UUID, thread_id: str
) -> TripState:
    """The trip form of the conversation, to prefill the chat's trip form.

    The LLM service fills it turn by turn as the user talks, so this is a plain read. Older
    conversations from before that have no form yet: the LLM service then reads the saved
    messages once (one LLM call).
    """
    conversation = await _owned(db, user_id, thread_id)
    state = TripState.model_validate(
        await llm_client.get_trip(client, thread_id=thread_id, user_id=str(user_id))
    )
    if state.info != TripInfo():
        return state

    messages = await _recent_messages(db, conversation, limit=TRIP_INFO_MESSAGES)
    if not any(m["role"] == "human" for m in messages):
        return state
    info = TripInfo.model_validate(await llm_client.extract_trip_info(client, messages=messages))
    return TripState(info=info, missing=_missing(info))


async def confirm_trip(
    *,
    db: AsyncSession,
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    thread_id: str,
    info: TripInfo,
) -> TripState:
    """Save the form as the user confirmed it, before the plan is built from it."""
    await _owned(db, user_id, thread_id)
    body = await llm_client.confirm_trip(
        client, thread_id=thread_id, user_id=str(user_id), info=info.model_dump(mode="json")
    )
    return TripState.model_validate(body)


async def plan_in_chat(
    *,
    db: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    user_id: uuid.UUID,
    thread_id: str,
    data: ChatPlanCreate,
) -> ChatPlanReply:
    """Build the plan from the chat's form and keep it in the conversation.

    The confirmed form is saved first (so later turns use exactly it), then the plan runs
    under its own plan thread id (progress on `GET /plan/{id}/events`). On success the form
    and the plan, both as text, are saved as a user message and an answer — reopening the
    conversation shows them, and the assistant can talk about the plan.
    """
    conversation = await _owned(db, user_id, thread_id)
    if data.trip is not None:
        await llm_client.confirm_trip(
            client,
            thread_id=thread_id,
            user_id=str(user_id),
            info=data.trip.model_dump(mode="json"),
        )
    plan_thread_id = data.plan_thread_id or plan_service.new_thread_id()
    form = {k: v for k, v in data.form.items() if k not in ("thread_id", "user_id")}
    async with plan_service.claimed(redis, user_id, plan_thread_id):
        body = await llm_client.chat_plan(
            client,
            chat_thread_id=thread_id,
            form=form,
            plan_thread_id=plan_thread_id,
            user_id=str(user_id),
        )
    reply = ChatPlanReply.model_validate(body)
    if reply.plan.get("status") != "ok":
        return reply  # questions first; the conversation gets the plan once there is one
    _append(db, conversation, "human", reply.request_text)
    # Keep the whole plan with the answer: reopening the chat shows the 3 options again.
    _append(db, conversation, "ai", reply.reply, plan={"form": form, "result": reply.plan})
    await db.commit()
    return reply


async def rename_thread(
    *, db: AsyncSession, user_id: uuid.UUID, thread_id: str, title: str
) -> ChatThread:
    conversation = await _owned(db, user_id, thread_id)
    conversation.title = _title(title)
    await db.commit()
    return _thread(conversation)


async def delete_thread(*, db: AsyncSession, user_id: uuid.UUID, thread_id: str) -> None:
    """Delete the conversation and its messages. The LLM service's memory expires on its own."""
    conversation = await _owned(db, user_id, thread_id)
    # Explicit rather than relying on ON DELETE CASCADE (SQLite in tests ignores it).
    await db.execute(delete(Message).where(Message.conversation_id == conversation.id))
    await db.delete(conversation)
    await db.commit()


# ---------------------------------------------------------------- streaming


async def _relay_and_save(
    response: httpx.Response,
    session_factory: async_sessionmaker[AsyncSession],
    conversation_id: str,
) -> AsyncIterator[bytes]:
    """Forward the SSE bytes unchanged while collecting the answer text, then save it."""
    parts: list[str] = []
    buffer = b""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
            buffer += chunk
            # Each SSE event ends with a blank line; parse whole events only.
            while b"\n\n" in buffer:
                frame, buffer = buffer.split(b"\n\n", 1)
                text = _delta_text(frame)
                if text:
                    parts.append(text)
    finally:
        answer = "".join(parts)
        if answer:
            # Shielded: if the client disconnected, this coroutine is being cancelled, and
            # the part of the answer they already saw should still be saved.
            await asyncio.shield(_save_answer(session_factory, conversation_id, answer))


def _delta_text(frame: bytes) -> str | None:
    for line in frame.decode("utf-8", errors="replace").splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except json.JSONDecodeError:
            return None
        if isinstance(event, dict) and event.get("type") == "delta":
            text = event.get("text")
            return text if isinstance(text, str) else None
    return None


async def _save_answer(
    session_factory: async_sessionmaker[AsyncSession], conversation_id: str, answer: str
) -> None:
    try:
        async with session_factory() as db:
            conversation = await db.get(Conversation, conversation_id)
            if conversation is None:  # deleted while the answer was streaming
                return
            _append(db, conversation, "ai", answer)
            await db.commit()
    except Exception:
        # The user already has the answer on screen; losing the saved copy is bad but
        # must not turn a finished stream into an error.
        logger.exception("Could not save the answer for conversation %s", conversation_id)


# ---------------------------------------------------------------- helpers

# Required to build a plan, in asking order — mirrors `REQUIRED` in the LLM service. Only
# used for older conversations whose form was read in one go (`trip_info` fallback).
REQUIRED_FIELDS = {
    "destination": "điểm đến",
    "start_date": "ngày đi",
    "days": "số ngày",
    "party": "số người đi",
    "budget_million": "ngân sách",
}


def _missing(info: TripInfo) -> list[MissingField]:
    empty = {
        "destination": info.destination is None,
        "start_date": info.start_date is None,
        "days": info.days is None,
        "party": info.adults is None and info.children is None and info.seniors is None,
        "budget_million": info.budget_million is None,
    }
    return [MissingField(field=k, label=v) for k, v in REQUIRED_FIELDS.items() if empty[k]]


def _new_thread_id() -> str:
    return f"chat-{uuid.uuid4().hex[:12]}"


def _new_conversation(thread_id: str, user_id: uuid.UUID, first_message: str) -> Conversation:
    now = datetime.now(UTC)
    return Conversation(
        id=thread_id,
        user_id=user_id,
        title=_title(first_message),
        created_at=now,
        updated_at=now,
        last_active_at=now,
    )


def _title(text: str) -> str:
    """One line, at most TITLE_MAX_CHARS, cut at a word boundary when possible."""
    one_line = " ".join(text.split())
    if len(one_line) <= TITLE_MAX_CHARS:
        return one_line
    cut = one_line[: TITLE_MAX_CHARS - 1]
    if " " in cut[TITLE_MAX_CHARS // 2 :]:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _append(
    db: AsyncSession,
    conversation: Conversation,
    role: str,
    content: str,
    *,
    plan: dict[str, Any] | None = None,
) -> None:
    now = datetime.now(UTC)
    db.add(
        Message(
            conversation_id=conversation.id,
            role=role,
            content=content,
            plan=plan,
            created_at=now,
        )
    )
    conversation.last_active_at = now


async def _recent_messages(
    db: AsyncSession, conversation: Conversation | None, *, limit: int = CONTEXT_MESSAGES
) -> list[dict[str, str]]:
    """The last few messages, oldest first, in the shape the LLM service accepts."""
    if conversation is None:
        return []
    rows: Sequence[Message] = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.id.desc())
            .limit(limit)
        )
    ).all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


async def _owned(db: AsyncSession, user_id: uuid.UUID, thread_id: str | None) -> Conversation:
    """The conversation, if it belongs to this user.

    404 rather than 403: someone else's thread id should look like it does not exist.
    """
    conversation = await db.get(Conversation, thread_id) if thread_id else None
    if conversation is None or conversation.user_id != user_id:
        raise NotFoundError("Conversation not found")
    return conversation


def _thread(conversation: Conversation) -> ChatThread:
    return ChatThread(
        thread_id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        last_active_at=conversation.last_active_at,
    )


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
