"""Chat routes, with the LLM service faked.

`llm_transport` answers in-process, so these tests never open a socket and do not need
the LLM service running.
"""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import get_llm_client
from app.main import app
from app.models.user import User
from tests.conftest import auth_header, login

UPSTREAM_REPLY = "Đà Lạt 3 ngày thì nên đi Puppy Farm và Dinh 1."


def fake_llm(handler: object) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://llm.test")


def default_handler(request: httpx.Request) -> httpx.Response:
    """Mimics the LLM service: echoes/creates a thread_id and answers."""
    if request.url.path == "/chat":
        body = json.loads(request.content)
        thread_id = body.get("thread_id") or "chat-upstream-new"
        return httpx.Response(200, json={"thread_id": thread_id, "reply": UPSTREAM_REPLY})
    if request.url.path.startswith("/chat/"):
        thread_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200,
            json={
                "thread_id": thread_id,
                "messages": [
                    {"role": "human", "content": "Đi Đà Lạt 3 ngày?"},
                    {"role": "ai", "content": UPSTREAM_REPLY},
                ],
            },
        )
    return httpx.Response(404)


@pytest.fixture
def use_llm(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Override the LLM client dependency with a handler (default: `default_handler`)."""
    handler = getattr(request, "param", default_handler)
    client = fake_llm(handler)
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def auth(client: AsyncClient, user: User) -> dict[str, str]:
    tokens = await login(client, user.email)
    return auth_header(tokens["access_token"])


# ---------------------------------------------------------------- happy path
async def test_send_message_creates_thread(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    resp = await client.post("/api/v1/chat", json={"message": "Đi Đà Lạt 3 ngày?"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == UPSTREAM_REPLY
    assert body["thread_id"] == "chat-upstream-new"


async def test_second_turn_reuses_thread(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    first = await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
    thread_id = first.json()["thread_id"]

    second = await client.post(
        "/api/v1/chat",
        json={"message": "Ngày 2 đi đâu?", "thread_id": thread_id},
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["thread_id"] == thread_id


async def test_list_and_get_history(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    created = await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
    thread_id = created.json()["thread_id"]

    listed = await client.get("/api/v1/chat/threads", headers=headers)
    assert listed.status_code == 200
    assert [t["thread_id"] for t in listed.json()["items"]] == [thread_id]
    assert listed.json()["total"] == 1

    history = await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)
    assert history.status_code == 200
    assert [m["role"] for m in history.json()["messages"]] == ["human", "ai"]


async def test_delete_thread(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    thread_id = (
        await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
    ).json()["thread_id"]

    deleted = await client.delete(f"/api/v1/chat/threads/{thread_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get("/api/v1/chat/threads", headers=headers)).json()["total"] == 0


async def test_stream_forwards_sse(client: AsyncClient, user: User) -> None:
    def streaming_handler(request: httpx.Request) -> httpx.Response:
        chunks = [
            b'data: {"type": "start"}\n\n',
            b'data: {"type": "delta", "text": "Xin "}\n\n',
            b'data: {"type": "delta", "text": "ch\xc3\xa0o"}\n\n',
            b'data: {"type": "done"}\n\n',
        ]
        return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks)))

    llm = fake_llm(streaming_handler)
    app.dependency_overrides[get_llm_client] = lambda: llm
    try:
        headers = await auth(client, user)
        async with client.stream(
            "POST", "/api/v1/chat/stream", json={"message": "chào"}, headers=headers
        ) as resp:
            assert resp.status_code == 200
            text = "".join([chunk async for chunk in resp.aiter_text()])
        assert '"type": "delta", "text": "Xin "' in text
        assert text.count("data:") == 4
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


# ---------------------------------------------------------------- auth & ownership
async def test_requires_auth(client: AsyncClient, use_llm: None) -> None:
    resp = await client.post("/api/v1/chat", json={"message": "chào"})
    assert resp.status_code == 401


async def test_cannot_use_someone_elses_thread(
    client: AsyncClient, user: User, superuser: User, use_llm: None
) -> None:
    owner_headers = await auth(client, user)
    thread_id = (
        await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=owner_headers)
    ).json()["thread_id"]

    other_headers = await auth(client, superuser)
    resp = await client.post(
        "/api/v1/chat", json={"message": "cho xem", "thread_id": thread_id}, headers=other_headers
    )
    assert resp.status_code == 404

    assert (
        await client.get(f"/api/v1/chat/threads/{thread_id}", headers=other_headers)
    ).status_code == 404


async def test_threads_are_per_user(
    client: AsyncClient, user: User, superuser: User, use_llm: None
) -> None:
    await client.post(
        "/api/v1/chat", json={"message": "Xin chào"}, headers=await auth(client, user)
    )
    other = await client.get("/api/v1/chat/threads", headers=await auth(client, superuser))
    assert other.json()["total"] == 0


# ---------------------------------------------------------------- validation & upstream errors
async def test_empty_message_rejected(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    assert (
        await client.post("/api/v1/chat", json={"message": ""}, headers=headers)
    ).status_code == 422


@pytest.mark.parametrize(
    "use_llm",
    [lambda request: httpx.Response(500, text="boom")],
    indirect=True,
)
async def test_upstream_error_becomes_503(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    resp = await client.post("/api/v1/chat", json={"message": "chào"}, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


async def test_unreachable_llm_becomes_503(client: AsyncClient, user: User) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    llm = fake_llm(refuse)
    app.dependency_overrides[get_llm_client] = lambda: llm
    try:
        headers = await auth(client, user)
        resp = await client.post("/api/v1/chat", json={"message": "chào"}, headers=headers)
        assert resp.status_code == 503
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_failed_turn_is_not_listed(client: AsyncClient, user: User) -> None:
    """A thread is only remembered once upstream actually answered."""
    llm = fake_llm(lambda request: httpx.Response(500, text="boom"))
    app.dependency_overrides[get_llm_client] = lambda: llm
    try:
        headers = await auth(client, user)
        await client.post("/api/v1/chat", json={"message": "chào"}, headers=headers)
        listed = await client.get("/api/v1/chat/threads", headers=headers)
        assert listed.json()["total"] == 0
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_redis_index_is_namespaced_per_user(
    client: AsyncClient, user: User, redis: FakeAsyncRedis, use_llm: None, db: AsyncSession
) -> None:
    headers = await auth(client, user)
    await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
    assert await redis.exists(f"chat:threads:{user.id}")
    assert await redis.ttl(f"chat:threads:{user.id}") > 0
