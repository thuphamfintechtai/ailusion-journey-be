import uuid

from httpx import AsyncClient

from app.models.user import User
from tests.conftest import auth_header, login


async def test_update_me(client: AsyncClient, user: User) -> None:
    tokens = await login(client, user.email)
    headers = auth_header(tokens["access_token"])

    resp = await client.patch(
        "/api/v1/users/me",
        json={"full_name": "Updated Name", "password": "An0ther-password!"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["full_name"] == "Updated Name"

    # New password works, old one does not.
    await login(client, user.email, "An0ther-password!")
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": "Str0ng-password!"}
    )
    assert resp.status_code == 401


async def test_list_users_forbidden_for_regular_user(client: AsyncClient, user: User) -> None:
    tokens = await login(client, user.email)
    resp = await client.get("/api/v1/users", headers=auth_header(tokens["access_token"]))
    assert resp.status_code == 403


async def test_list_users_as_superuser(client: AsyncClient, user: User, superuser: User) -> None:
    tokens = await login(client, superuser.email)
    resp = await client.get(
        "/api/v1/users", params={"limit": 10}, headers=auth_header(tokens["access_token"])
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 2
    assert {u["email"] for u in body["items"]} == {user.email, superuser.email}


async def test_get_user_by_id(client: AsyncClient, user: User, superuser: User) -> None:
    tokens = await login(client, superuser.email)
    headers = auth_header(tokens["access_token"])

    resp = await client.get(f"/api/v1/users/{user.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == str(user.id)

    resp = await client.get(f"/api/v1/users/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
