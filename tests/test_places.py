"""Place-detail route, with the LLM service faked (no socket, no LLM service needed)."""

from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import AsyncClient

from app.core.llm import get_llm_client
from app.main import app
from app.models.user import User
from tests.test_chat import auth, fake_llm

PLACE = {
    "place_id": "ChIJ1",
    "name": "Hồ Xuân Hương",
    "images": ["https://img/t.jpg"],
    "reviews": [{"rating": 5, "date": "2026-09-01", "text": "đẹp", "images": []}],
}

seen: list[httpx.Request] = []


def place_handler(request: httpx.Request) -> httpx.Response:
    """Mimics the LLM service: GET /places/{id}."""
    seen.append(request)
    if request.url.path == "/places/ChIJ1":
        return httpx.Response(200, json=PLACE)
    if request.url.path == "/places/ChIJbroken":
        return httpx.Response(502, json={"detail": "Không lấy được chi tiết địa điểm"})
    return httpx.Response(404, json={"detail": "Không tìm thấy địa điểm"})


@pytest.fixture
def use_place_llm() -> AsyncIterator[None]:
    seen.clear()
    client = fake_llm(place_handler)
    app.dependency_overrides[get_llm_client] = lambda: client
    yield
    app.dependency_overrides.pop(get_llm_client, None)


async def test_get_place_passes_through(
    client: AsyncClient, user: User, use_place_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.get(
        "/api/v1/places/ChIJ1", params={"destination": "Đà Lạt"}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == PLACE
    assert seen[0].url.params["destination"] == "Đà Lạt"
    assert "review_photos" not in seen[0].url.params


async def test_get_place_forwards_review_photos(
    client: AsyncClient, user: User, use_place_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.get(
        "/api/v1/places/ChIJ1", params={"review_photos": "true"}, headers=headers
    )

    assert resp.status_code == 200, resp.text
    assert seen[0].url.params["review_photos"] == "true"
    assert "destination" not in seen[0].url.params


async def test_get_place_requires_login(client: AsyncClient, use_place_llm: None) -> None:
    resp = await client.get("/api/v1/places/ChIJ1")

    assert resp.status_code == 401
    assert seen == []


async def test_unknown_place_is_404(client: AsyncClient, user: User, use_place_llm: None) -> None:
    headers = await auth(client, user)
    resp = await client.get("/api/v1/places/ChIJnope", headers=headers)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Không tìm thấy địa điểm"


async def test_upstream_error_is_503(client: AsyncClient, user: User, use_place_llm: None) -> None:
    headers = await auth(client, user)
    resp = await client.get("/api/v1/places/ChIJbroken", headers=headers)

    assert resp.status_code == 503
    assert resp.json()["code"] == "upstream_unavailable"


async def test_place_id_cannot_reach_other_routes(
    client: AsyncClient, user: User, use_place_llm: None
) -> None:
    headers = await auth(client, user)
    resp = await client.get("/api/v1/places/..%2Fchat%2Fx", headers=headers)

    assert resp.status_code in (404, 422)
    assert seen == []
