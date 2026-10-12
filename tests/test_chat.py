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

EMPTY_TRIP = {
    "info": {"access": [], "interests": []},
    "missing": [{"field": "destination", "label": "điểm đến"}],
    "wants_plan": False,
    "open_form": False,
}


def fake_llm(handler: object) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport, base_url="http://llm.test")


def default_handler(request: httpx.Request) -> httpx.Response:
    """Mimics the LLM service: echoes/creates a thread_id and answers."""
    if request.url.path == "/chat":
        body = json.loads(request.content)
        thread_id = body.get("thread_id") or "chat-upstream-new"
        return httpx.Response(200, json={"thread_id": thread_id, "reply": UPSTREAM_REPLY})
    if request.url.path.startswith("/chat/") and request.url.path.endswith("/trip"):
        return httpx.Response(200, json=EMPTY_TRIP)
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
    # The backend names new conversations (so it can save them), not the LLM service.
    assert body["thread_id"].startswith("chat-")
    assert body["thread_id"] != "chat-upstream-new"


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


# ---------------------------------------------------------------- durable history (PostgreSQL)
def streaming_reply(*texts: str) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        handler.requests.append(body)  # type: ignore[attr-defined]
        events = [{"type": "start", "thread_id": body["thread_id"]}]
        events += [{"type": "delta", "text": t} for t in texts]
        events.append({"type": "done", "thread_id": body["thread_id"]})
        raw = b"".join(f"data: {json.dumps(e)}\n\n".encode() for e in events)
        return httpx.Response(200, stream=httpx.ByteStream(raw))

    handler.requests = []  # type: ignore[attr-defined]
    return handler


async def stream(client: AsyncClient, headers: dict[str, str], body: dict[str, str]) -> str:
    async with client.stream("POST", "/api/v1/chat/stream", json=body, headers=headers) as resp:
        assert resp.status_code == 200
        return "".join([chunk async for chunk in resp.aiter_text()])


async def test_stream_saves_question_and_answer(client: AsyncClient, user: User) -> None:
    handler = streaming_reply("Đà Lạt ", "rất đẹp.")
    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        text = await stream(client, headers, {"message": "Đi Đà Lạt 3 ngày nên đi đâu?"})
        thread_id = json.loads(text.split("\n\n")[0][5:])["thread_id"]

        threads = (await client.get("/api/v1/chat/threads", headers=headers)).json()
        assert threads["total"] == 1
        assert threads["items"][0]["title"] == "Đi Đà Lạt 3 ngày nên đi đâu?"

        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()
        assert [(m["role"], m["content"]) for m in history["messages"]] == [
            ("human", "Đi Đà Lạt 3 ngày nên đi đâu?"),
            ("ai", "Đà Lạt rất đẹp."),
        ]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_next_turn_sends_saved_history(client: AsyncClient, user: User) -> None:
    """The LLM service gets the saved messages, to rebuild a memory that has expired."""
    handler = streaming_reply("Trả lời.")
    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        first = await stream(client, headers, {"message": "Câu một"})
        thread_id = json.loads(first.split("\n\n")[0][5:])["thread_id"]
        await stream(client, headers, {"message": "Câu hai", "thread_id": thread_id})

        sent = handler.requests  # type: ignore[attr-defined]
        assert "history" not in sent[0]
        assert sent[1]["history"] == [
            {"role": "human", "content": "Câu một"},
            {"role": "ai", "content": "Trả lời."},
        ]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_retrying_unanswered_question_is_not_saved_twice(
    client: AsyncClient, user: User
) -> None:
    silent = streaming_reply()  # upstream ends without any text
    app.dependency_overrides[get_llm_client] = lambda: fake_llm(silent)
    try:
        headers = await auth(client, user)
        first = await stream(client, headers, {"message": "Hỏi lại nhé"})
        thread_id = json.loads(first.split("\n\n")[0][5:])["thread_id"]

        answering = streaming_reply("Đây.")
        app.dependency_overrides[get_llm_client] = lambda: fake_llm(answering)
        await stream(client, headers, {"message": "Hỏi lại nhé", "thread_id": thread_id})

        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()
        assert [m["role"] for m in history["messages"]] == ["human", "ai"]
        assert "history" not in answering.requests[0]  # type: ignore[attr-defined]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_question_is_saved_even_when_the_llm_service_fails(
    client: AsyncClient, user: User
) -> None:
    """The request reaches the LLM service before the question is committed; a failing
    LLM service must still leave the question in the history."""

    def down(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chat/stream":
            return httpx.Response(503, json={"detail": "down"})
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(default_handler)
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
        ).json()["thread_id"]
        app.dependency_overrides[get_llm_client] = lambda: fake_llm(down)
        async with client.stream(
            "POST",
            "/api/v1/chat/stream",
            json={"message": "Còn ở đó không?", "thread_id": thread_id},
            headers=headers,
        ) as resp:
            body = (await resp.aread()).decode()
        # Lỗi đến sau khi stream đã mở: báo bằng sự kiện `error`, không cắt ngang stream.
        assert '"type": "error"' in body

        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()
        assert [m["content"] for m in history["messages"]][-1] == "Còn ở đó không?"
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_rename_thread(client: AsyncClient, user: User, use_llm: None) -> None:
    headers = await auth(client, user)
    thread_id = (
        await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
    ).json()["thread_id"]

    renamed = await client.patch(
        f"/api/v1/chat/threads/{thread_id}", json={"title": "Đà Lạt tháng 12"}, headers=headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Đà Lạt tháng 12"
    listed = (await client.get("/api/v1/chat/threads", headers=headers)).json()
    assert listed["items"][0]["title"] == "Đà Lạt tháng 12"

    blank = await client.patch(
        f"/api/v1/chat/threads/{thread_id}", json={"title": ""}, headers=headers
    )
    assert blank.status_code == 422


async def test_trip_info_reads_the_saved_conversation(client: AsyncClient, user: User) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trip-info":
            seen.append(json.loads(request.content))
            return httpx.Response(
                200, json={"destination": "Đà Lạt", "days": 3, "interests": ["Ẩm thực"]}
            )
        return default_handler(request)

    llm = fake_llm(handler)
    app.dependency_overrides[get_llm_client] = lambda: llm
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post(
                "/api/v1/chat", json={"message": "Đi Đà Lạt 3 ngày?"}, headers=headers
            )
        ).json()["thread_id"]

        resp = await client.get(f"/api/v1/chat/threads/{thread_id}/trip-info", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["info"]["destination"] == "Đà Lạt"
        assert body["info"]["days"] == 3
        assert body["info"]["adults"] is None
        assert body["info"]["access"] == []
        assert [m["field"] for m in body["missing"]] == ["start_date", "party", "budget_million"]
        assert seen[0]["messages"] == [
            {"role": "human", "content": "Đi Đà Lạt 3 ngày?"},
            {"role": "ai", "content": UPSTREAM_REPLY},
        ]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_trip_info_uses_the_form_filled_during_the_chat(
    client: AsyncClient, user: User
) -> None:
    """The LLM service already keeps the form: no second read of the whole conversation."""
    paths: list[str] = []
    stored = {
        "info": {"destination": "Huế", "days": 2, "access": [], "interests": []},
        "missing": [{"field": "start_date", "label": "ngày đi"}],
        "wants_plan": True,
        "open_form": False,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/trip"):
            assert request.url.params["user_id"] == str(user.id)
            return httpx.Response(200, json=stored)
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post("/api/v1/chat", json={"message": "Đi Huế"}, headers=headers)
        ).json()["thread_id"]
        resp = await client.get(f"/api/v1/chat/threads/{thread_id}/trip-info", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["info"]["destination"] == "Huế"
        assert resp.json()["wants_plan"] is True
        assert "/trip-info" not in paths
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_confirm_trip_forwards_the_form(client: AsyncClient, user: User) -> None:
    sent: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/trip"):
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={**EMPTY_TRIP, "info": body["info"], "missing": []})
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post("/api/v1/chat", json={"message": "Đi Huế"}, headers=headers)
        ).json()["thread_id"]
        form = {
            "destination": "Huế",
            "start_date": "2026-12-20",
            "days": 2,
            "adults": 2,
            "budget_million": 6,
            "access": ["Ăn chay"],
        }
        resp = await client.put(
            f"/api/v1/chat/threads/{thread_id}/trip-info", json=form, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["info"]["destination"] == "Huế"
        assert sent[0]["user_id"] == str(user.id)
        assert sent[0]["info"]["access"] == ["Ăn chay"]  # type: ignore[index]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_confirm_trip_of_someone_elses_thread_is_404(
    client: AsyncClient, user: User, use_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.put(
        "/api/v1/chat/threads/chat-nobody/trip-info", json={"days": 2}, headers=headers
    )
    assert resp.status_code == 404


async def test_trip_info_of_someone_elses_thread_is_404(
    client: AsyncClient, user: User, use_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.get("/api/v1/chat/threads/chat-nobody/trip-info", headers=headers)
    assert resp.status_code == 404


async def test_search_threads_by_title_and_content(
    client: AsyncClient, user: User, use_llm: None
) -> None:
    headers = await auth(client, user)
    await client.post("/api/v1/chat", json={"message": "Đi Hội An"}, headers=headers)
    await client.post("/api/v1/chat", json={"message": "Đi Huế"}, headers=headers)

    by_title = (await client.get("/api/v1/chat/threads?q=hội an", headers=headers)).json()
    assert [t["title"] for t in by_title["items"]] == ["Đi Hội An"]

    # UPSTREAM_REPLY mentions "Puppy Farm" — found through the answers, in both threads.
    by_answer = (await client.get("/api/v1/chat/threads?q=puppy", headers=headers)).json()
    assert by_answer["total"] == 2

    none = (await client.get("/api/v1/chat/threads?q=100%", headers=headers)).json()
    assert none["total"] == 0


async def test_long_first_message_gives_short_title(
    client: AsyncClient, user: User, use_llm: None
) -> None:
    headers = await auth(client, user)
    long = "Lên lịch trình " + "rất chi tiết " * 20
    await client.post("/api/v1/chat", json={"message": long}, headers=headers)
    title = (await client.get("/api/v1/chat/threads", headers=headers)).json()["items"][0]["title"]
    assert len(title) <= 80
    assert title.endswith("…")


async def test_delete_removes_messages(
    client: AsyncClient, user: User, use_llm: None, db: AsyncSession
) -> None:
    from sqlalchemy import func, select

    from app.models.chat import Message

    headers = await auth(client, user)
    thread_id = (
        await client.post("/api/v1/chat", json={"message": "Xin chào"}, headers=headers)
    ).json()["thread_id"]
    await client.delete(f"/api/v1/chat/threads/{thread_id}", headers=headers)

    remaining = await db.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == thread_id)
    )
    assert remaining == 0
    assert (
        await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)
    ).status_code == 404


PLAN_THREAD = "plan-0123456789abcdef"


async def test_plan_in_chat_keeps_the_plan_in_the_conversation(
    client: AsyncClient, user: User, redis: FakeAsyncRedis
) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/plan") and path.startswith("/chat/"):
            body = json.loads(request.content)
            calls.append(("plan", path, body))
            return httpx.Response(
                200,
                json={
                    "plan": {"thread_id": body["thread_id"], "status": "ok", "destination": "Huế"},
                    "request_text": "Lập lộ trình: Huế, 2 ngày",
                    "reply": "**Lộ trình Huế 2 ngày**",
                },
            )
        if request.method == "PUT" and path.endswith("/trip"):
            calls.append(("confirm", path, json.loads(request.content)))
            return httpx.Response(200, json=EMPTY_TRIP)
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post("/api/v1/chat", json={"message": "Đi Huế"}, headers=headers)
        ).json()["thread_id"]
        resp = await client.post(
            f"/api/v1/chat/threads/{thread_id}/plan",
            json={
                "form": {
                    "destination": "Huế",
                    "start_date": "2026-12-20",
                    "end_date": "2026-12-21",
                    "group_size": 2,
                },
                "plan_thread_id": PLAN_THREAD,
                "trip": {"destination": "Huế", "days": 2},
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["plan"]["destination"] == "Huế"

        kinds = [c[0] for c in calls]
        assert kinds == ["confirm", "plan"]
        plan_body = calls[1][2]
        assert calls[1][1] == f"/chat/{thread_id}/plan"
        assert plan_body["thread_id"] == PLAN_THREAD
        assert plan_body["user_id"] == str(user.id)
        assert plan_body["destination"] == "Huế" and plan_body["group_size"] == 2
        # The plan thread is the user's, so its progress stream can be read.
        assert await redis.zscore(f"plan:threads:{user.id}", PLAN_THREAD) is not None

        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()[
            "messages"
        ]
        assert [m["content"] for m in history[-2:]] == [
            "Lập lộ trình: Huế, 2 ngày",
            "**Lộ trình Huế 2 ngày**",
        ]
        # The full plan comes back with the answer, so the client can show it again.
        assert history[-2]["plan"] is None
        assert history[-1]["plan"]["result"]["destination"] == "Huế"
        assert history[-1]["plan"]["form"]["destination"] == "Huế"
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_failed_plan_in_chat_saves_nothing_and_releases_the_plan_thread(
    client: AsyncClient, user: User, redis: FakeAsyncRedis
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/plan"):
            return httpx.Response(500, text="boom")
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post("/api/v1/chat", json={"message": "Đi Huế"}, headers=headers)
        ).json()["thread_id"]
        resp = await client.post(
            f"/api/v1/chat/threads/{thread_id}/plan",
            json={"form": {"destination": "Huế"}, "plan_thread_id": PLAN_THREAD},
            headers=headers,
        )
        assert resp.status_code == 503
        assert await redis.zscore(f"plan:threads:{user.id}", PLAN_THREAD) is None
        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()[
            "messages"
        ]
        assert len(history) == 2
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_plan_questions_are_saved_with_their_buttons(
    client: AsyncClient, user: User, redis: FakeAsyncRedis
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/plan") and request.url.path.startswith("/chat/"):
            return httpx.Response(
                200,
                json={
                    "plan": {"status": "need_info", "questions": [{"id": "H1"}]},
                    "request_text": "Lập lộ trình: Huế, 2 ngày",
                    "reply": "Mình cần thêm vài thông tin: Bạn đi cùng ai?",
                },
            )
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        thread_id = (
            await client.post("/api/v1/chat", json={"message": "Đi Huế"}, headers=headers)
        ).json()["thread_id"]
        resp = await client.post(
            f"/api/v1/chat/threads/{thread_id}/plan",
            json={"form": {"destination": "Huế"}, "plan_thread_id": PLAN_THREAD},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["plan"]["status"] == "need_info"
        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()[
            "messages"
        ]
        # Lượt chat ban đầu + yêu cầu lập lộ trình + câu hỏi lại (kèm kết quả để hiện nút chọn).
        assert [m["role"] for m in history] == ["human", "ai", "human", "ai"]
        assert history[3]["content"].startswith("Mình cần thêm vài thông tin")
        assert history[3]["plan"]["result"]["status"] == "need_info"
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_plan_from_an_empty_chat_starts_a_conversation(
    client: AsyncClient, user: User
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/plan") and path.startswith("/chat/"):
            seen.append(path)
            return httpx.Response(
                200,
                json={
                    "plan": {"status": "ok", "destination": "Huế"},
                    "request_text": "Lập lộ trình: Huế, 2 ngày",
                    "reply": "**Lộ trình Huế 2 ngày**",
                },
            )
        return default_handler(request)

    app.dependency_overrides[get_llm_client] = lambda: fake_llm(handler)
    try:
        headers = await auth(client, user)
        resp = await client.post(
            "/api/v1/chat/plan",
            json={
                "form": {
                    "destination": "Huế",
                    "start_date": "2026-12-20",
                    "end_date": "2026-12-21",
                    "group_size": 2,
                }
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        thread_id = resp.json()["thread_id"]
        assert thread_id.startswith("chat-") and seen == [f"/chat/{thread_id}/plan"]
        threads = (await client.get("/api/v1/chat/threads", headers=headers)).json()["items"]
        assert threads[0]["thread_id"] == thread_id
        assert threads[0]["title"] == "Lập lộ trình Huế"
        history = (await client.get(f"/api/v1/chat/threads/{thread_id}", headers=headers)).json()
        assert [m["role"] for m in history["messages"]] == ["human", "ai"]
    finally:
        app.dependency_overrides.pop(get_llm_client, None)


async def test_plan_in_someone_elses_chat_is_404(
    client: AsyncClient, user: User, use_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.post(
        "/api/v1/chat/threads/chat-nobody/plan", json={"form": {}}, headers=headers
    )
    assert resp.status_code == 404
