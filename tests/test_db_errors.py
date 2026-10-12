"""Losing the database connection mid-request gives a retryable 503 the browser can read."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError, OperationalError

from app.core.exceptions import register_exception_handlers


def _app(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise exc

    return app


async def _get(app: FastAPI) -> tuple[int, dict[str, str]]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/boom")
    return resp.status_code, resp.json()


async def test_lost_connection_is_503() -> None:
    lost = DBAPIError(
        "SELECT 1", {}, Exception("connection was closed"), connection_invalidated=True
    )
    status, body = await _get(_app(lost))
    assert status == 503
    assert body["code"] == "database_unavailable"


async def test_operational_error_is_503() -> None:
    status, body = await _get(_app(OperationalError("SELECT 1", {}, Exception("timeout"))))
    assert status == 503
    assert body["code"] == "database_unavailable"


async def test_other_database_errors_stay_500() -> None:
    status, body = await _get(_app(DBAPIError("SELECT 1", {}, Exception("syntax error"))))
    assert status == 500
    assert body["code"] == "internal_error"
