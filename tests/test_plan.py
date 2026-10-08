"""Trip-plan routes, with the LLM service faked (no socket, no LLM service needed)."""

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from httpx import AsyncClient

from app.core.llm import get_llm_client
from app.main import app
from app.models.user import User
from tests.test_chat import auth, fake_llm

THREAD_ID = "plan-0123456789abcdef"
FORM = {
    "persona": "P2",
    "basic": {"destination": "Đà Lạt", "start_date": "2026-10-17", "days": 3, "children": 1},
    "budget": {"total_vnd": 20000000, "hard": False},
}

# What the faked LLM service received, per test.
seen: list[dict[str, object]] = []


def plan_handler(request: httpx.Request) -> httpx.Response:
    """Mimics the LLM service: POST /plan returns a (trimmed) PlanResponse."""
    if request.url.path == "/plan":
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "thread_id": body["thread_id"],
                "persona": body.get("persona", "P1"),
                "decision": {"winner": "canbang"},
                "options": {},
            },
        )
    if request.url.path.endswith("/events"):
        frames = (
            b'id: 1-0\ndata: {"node": "turn", "kind": "start"}\n\n'
            b'id: 2-0\ndata: {"node": "chien_luoc", "kind": "plan", "text": "x"}\n\n'
            b'id: 3-0\ndata: {"node": "turn", "kind": "done"}\n\n'
        )
        seen.append({"last_event_id": request.headers.get("last-event-id")})
        return httpx.Response(200, stream=httpx.ByteStream(frames))
    return httpx.Response(404)


@pytest.fixture
def use_plan_llm() -> AsyncIterator[None]:
    seen.clear()
    client = fake_llm(plan_handler)
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


# ---------------------------------------------------------------- happy path
async def test_create_plan_forwards_only_sent_fields(
    client: AsyncClient, user: User, use_plan_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.post("/api/v1/plan", json=FORM, headers=headers)

    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"]["winner"] == "canbang"
    sent = seen[0]
    # Groups the client left out are not forwarded, so the LLM service can list them as
    # assumptions instead of seeing explicit nulls.
    assert set(sent) == {"persona", "basic", "budget", "thread_id", "user_id"}
    assert sent["user_id"] == str(user.id)
    assert str(sent["thread_id"]).startswith("plan-")


async def test_client_thread_id_is_used_and_owned(
    client: AsyncClient, user: User, redis: FakeAsyncRedis, use_plan_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.post("/api/v1/plan", json={**FORM, "thread_id": THREAD_ID}, headers=headers)

    assert resp.status_code == 200
    assert seen[0]["thread_id"] == THREAD_ID
    assert await redis.zscore(f"plan:threads:{user.id}", THREAD_ID) is not None
    assert await redis.zscore(f"chat:threads:{user.id}", THREAD_ID) is None


async def test_events_are_forwarded_with_last_event_id(
    client: AsyncClient, user: User, use_plan_llm: None
) -> None:
    headers = await auth(client, user)
    await client.post("/api/v1/plan", json={**FORM, "thread_id": THREAD_ID}, headers=headers)

    async with client.stream(
        "GET",
        f"/api/v1/plan/{THREAD_ID}/events",
        headers={**headers, "Last-Event-ID": "1-0"},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join([chunk async for chunk in resp.aiter_bytes()])

    assert b'"node": "chien_luoc"' in body
    assert seen[-1] == {"last_event_id": "1-0"}


# ---------------------------------------------------------------- errors
async def test_plan_requires_auth(client: AsyncClient, use_plan_llm: None) -> None:
    assert (await client.post("/api/v1/plan", json=FORM)).status_code == 401


@pytest.mark.parametrize("thread_id", ["chat-0123456789abcdef", "plan-xyz", "plan-0123"])
async def test_bad_thread_id_is_rejected(
    client: AsyncClient, user: User, use_plan_llm: None, thread_id: str
) -> None:
    headers = await auth(client, user)
    resp = await client.post("/api/v1/plan", json={**FORM, "thread_id": thread_id}, headers=headers)
    assert resp.status_code == 422
    assert seen == []


async def test_unknown_persona_is_rejected(
    client: AsyncClient, user: User, use_plan_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.post("/api/v1/plan", json={**FORM, "persona": "P9"}, headers=headers)
    assert resp.status_code == 422


async def test_someone_elses_plan_events_look_missing(
    client: AsyncClient, user: User, superuser: User, use_plan_llm: None
) -> None:
    owner = await auth(client, user)
    await client.post("/api/v1/plan", json={**FORM, "thread_id": THREAD_ID}, headers=owner)

    stranger = await auth(client, superuser)
    resp = await client.get(f"/api/v1/plan/{THREAD_ID}/events", headers=stranger)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


async def test_upstream_form_error_becomes_422(client: AsyncClient, user: User) -> None:
    def rejects(_: httpx.Request) -> httpx.Response:
        detail = [{"loc": ["body", "basic", "days"], "msg": "Input should be less than 6"}]
        return httpx.Response(422, json={"detail": detail})

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(rejects)
    try:
        headers = await auth(client, user)
        resp = await client.post("/api/v1/plan", json=FORM, headers=headers)
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert resp.status_code == 422
    assert resp.json() == {
        "detail": "Invalid plan form: basic.days: Input should be less than 6",
        "code": "validation_error",
    }


async def test_failed_plan_releases_thread(
    client: AsyncClient, user: User, redis: FakeAsyncRedis
) -> None:
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(down)
    try:
        headers = await auth(client, user)
        resp = await client.post(
            "/api/v1/plan", json={**FORM, "thread_id": THREAD_ID}, headers=headers
        )
    finally:
        app.dependency_overrides.pop(get_llm_client, None)

    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"
    assert await redis.zscore(f"plan:threads:{user.id}", THREAD_ID) is None
