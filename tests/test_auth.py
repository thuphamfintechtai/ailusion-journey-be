from httpx import AsyncClient

from app.core.security import create_access_token
from app.models.user import User
from tests.conftest import DEFAULT_PASSWORD, auth_header, login


async def test_register(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "New.User@Example.com", "password": DEFAULT_PASSWORD, "full_name": "New"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "new.user@example.com"
    assert body["is_active"] is True
    assert body["is_superuser"] is False
    assert "password" not in body
    assert "hashed_password" not in body


async def test_register_duplicate_email(client: AsyncClient, user: User) -> None:
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": user.email.upper(), "password": DEFAULT_PASSWORD},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"


async def test_register_rejects_short_password(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/register", json={"email": "a@example.com", "password": "short"}
    )
    assert resp.status_code == 422


async def test_login_and_me(client: AsyncClient, user: User) -> None:
    tokens = await login(client, user.email)
    assert tokens["token_type"] == "bearer"
    assert tokens["refresh_token"]

    resp = await client.get("/api/v1/users/me", headers=auth_header(tokens["access_token"]))
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


async def test_login_wrong_password(client: AsyncClient, user: User) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "wrong-password"}
    )
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


async def test_login_unknown_email(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 401


async def test_oauth2_token_form(client: AsyncClient, user: User) -> None:
    resp = await client.post(
        "/api/v1/auth/token", data={"username": user.email, "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


async def test_me_requires_token(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401


async def test_me_rejects_invalid_token(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/users/me", headers=auth_header("not-a-jwt"))
    assert resp.status_code == 401


async def test_refresh_rotates_tokens(client: AsyncClient, user: User) -> None:
    tokens = await login(client, user.email)

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 200, resp.text
    new_tokens = resp.json()
    assert new_tokens["refresh_token"] != tokens["refresh_token"]

    # The old refresh token is revoked after rotation.
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_access_token_cannot_be_used_to_refresh(client: AsyncClient, user: User) -> None:
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": create_access_token(str(user.id))}
    )
    assert resp.status_code == 401


async def test_logout_revokes_refresh_token(client: AsyncClient, user: User) -> None:
    tokens = await login(client, user.email)

    resp = await client.post("/api/v1/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert resp.status_code == 204

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 401
