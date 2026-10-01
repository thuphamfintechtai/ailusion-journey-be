from httpx import AsyncClient


async def test_liveness(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert "X-Request-ID" in resp.headers


async def test_readiness(client: AsyncClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


async def test_request_id_is_echoed(client: AsyncClient) -> None:
    resp = await client.get("/health", headers={"X-Request-ID": "abc123"})
    assert resp.headers["X-Request-ID"] == "abc123"
