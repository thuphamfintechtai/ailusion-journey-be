from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header, Path
from fastapi.responses import StreamingResponse

from app.api.deps import CurrentUser, LlmClient, RedisClient
from app.schemas.plan import PLAN_THREAD_ID, PlanCreate
from app.services import plan_service

router = APIRouter(prefix="/plan", tags=["plan"])


@router.post("")
async def create_plan(
    data: PlanCreate, user: CurrentUser, redis: RedisClient, llm: LlmClient
) -> dict[str, Any]:
    """Fill the trip form, get back 3 options (balanced / budget / experience), the chosen
    one with an explanation, a day-by-day schedule and the assumptions made.

    Takes 20-60 s. Send a `thread_id` and open `GET /plan/{thread_id}/events` alongside to
    show progress. The response is the LLM service's `PlanResponse` unchanged.
    """
    return await plan_service.create_plan(redis=redis, client=llm, user_id=user.id, data=data)


@router.get("/{thread_id}/events")
async def stream_events(
    thread_id: Annotated[str, Path(pattern=PLAN_THREAD_ID)],
    user: CurrentUser,
    redis: RedisClient,
    llm: LlmClient,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Progress of a plan run as server-sent events, one per pipeline step.

    Event data: `{"ts", "node", "kind", "text", "data"}`; the stream closes after the run's
    `turn` event with kind `done` or `error`. Returns 404 until `POST /plan` with this
    `thread_id` has started, so open it right after sending the plan request (retry on 404).
    """

    async def forward() -> AsyncIterator[bytes]:
        async with plan_service.stream_events(
            redis=redis,
            client=llm,
            user_id=user.id,
            thread_id=thread_id,
            last_event_id=last_event_id,
        ) as upstream:
            async for chunk in upstream.aiter_bytes():
                yield chunk

    # Check ownership before the response starts, so a stranger gets a proper 404
    # instead of an empty 200 stream.
    await plan_service.ensure_owned(redis=redis, user_id=user.id, thread_id=thread_id)
    return StreamingResponse(
        forward(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
