"""Thin wrapper over the LLM service HTTP API (`ailusion-journey-llm`).

Knows the upstream URLs and nothing about our users or permissions — that is
`chat_service`'s job. Every network or non-2xx failure becomes an `UpstreamError`
so routes never leak an httpx exception.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.core.exceptions import UpstreamError

logger = logging.getLogger(__name__)


async def send_message(
    client: httpx.AsyncClient,
    *,
    message: str,
    thread_id: str | None,
    user_id: str,
) -> dict[str, Any]:
    """Send one turn, wait for the full answer. Returns `{"thread_id", "reply"}`."""
    return await _post_json("/chat", client, payload=_payload(message, thread_id, user_id))


async def get_history(client: httpx.AsyncClient, thread_id: str) -> dict[str, Any]:
    """Conversation history held by the LLM service. 404 upstream -> empty history."""
    try:
        response = await client.get(f"/chat/{thread_id}")
    except httpx.HTTPError as exc:
        raise _unavailable(exc) from exc

    if response.status_code == httpx.codes.NOT_FOUND:
        return {"thread_id": thread_id, "messages": []}
    return _parse(response)


@asynccontextmanager
async def stream_message(
    client: httpx.AsyncClient,
    *,
    message: str,
    thread_id: str | None,
    user_id: str,
) -> AsyncIterator[httpx.Response]:
    """Open the upstream SSE stream; the caller forwards `aiter_bytes()` to its client."""
    request = client.build_request(
        "POST", "/chat/stream", json=_payload(message, thread_id, user_id)
    )
    try:
        response = await client.send(request, stream=True)
    except httpx.HTTPError as exc:
        raise _unavailable(exc) from exc

    if response.status_code >= httpx.codes.BAD_REQUEST:
        await response.aread()
        await response.aclose()
        raise _bad_status(response)

    try:
        yield response
    finally:
        await response.aclose()


def _payload(message: str, thread_id: str | None, user_id: str) -> dict[str, Any]:
    # session_id groups a user's conversations together in Langfuse.
    payload: dict[str, Any] = {"message": message, "user_id": user_id, "session_id": user_id}
    if thread_id:
        payload["thread_id"] = thread_id
    return payload


async def _post_json(
    path: str, client: httpx.AsyncClient, *, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        response = await client.post(path, json=payload)
    except httpx.HTTPError as exc:
        raise _unavailable(exc) from exc
    return _parse(response)


def _parse(response: httpx.Response) -> dict[str, Any]:
    if response.status_code >= httpx.codes.BAD_REQUEST:
        raise _bad_status(response)
    body: dict[str, Any] = response.json()
    return body


def _unavailable(exc: httpx.HTTPError) -> UpstreamError:
    logger.warning("LLM service unreachable: %s: %s", type(exc).__name__, exc)
    return UpstreamError("Chat service is unavailable, please try again")


def _bad_status(response: httpx.Response) -> UpstreamError:
    logger.warning("LLM service returned %s: %s", response.status_code, response.text[:300])
    return UpstreamError("Chat service returned an error, please try again")
