from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.security import create_access_token, create_password_reset_token
from app.models.user import User
from app.services import email_service
from tests.conftest import DEFAULT_PASSWORD, login

FORGOT_URL = "/api/v1/auth/forgot-password"
RESET_URL = "/api/v1/auth/reset-password"
FORGOT_DETAIL = "Nếu email tồn tại, liên kết đặt lại mật khẩu đã được gửi."
NEW_PASSWORD = "Brand-new-passw0rd"


@pytest.fixture
def sent_emails(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Capture outgoing emails instead of sending/logging them."""
    sent: list[dict[str, str]] = []

    async def fake_send_email(to: str, subject: str, text: str, html: str | None = None) -> None:
        sent.append({"to": to, "subject": subject, "text": text})

    monkeypatch.setattr(email_service, "send_email", fake_send_email)
    return sent


def _token_from_email(text: str) -> str:
    link = next(line for line in text.splitlines() if "/reset-password?" in line).strip()
    assert link.startswith(f"{settings.FRONTEND_URL}/reset-password?token=")
    return parse_qs(urlparse(link).query)["token"][0]


async def _request_reset_token(client: AsyncClient, sent: list[dict[str, str]], email: str) -> str:
    resp = await client.post(FORGOT_URL, json={"email": email})
    assert resp.status_code == 202, resp.text
    return _token_from_email(sent[-1]["text"])


async def test_forgot_password_existing_email_sends_link(
    client: AsyncClient, user: User, sent_emails: list[dict[str, str]]
) -> None:
    resp = await client.post(FORGOT_URL, json={"email": user.email.upper()})
    assert resp.status_code == 202
    assert resp.json() == {"detail": FORGOT_DETAIL}

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == user.email
    assert _token_from_email(sent_emails[0]["text"])


async def test_forgot_password_unknown_email_same_response(
    client: AsyncClient, sent_emails: list[dict[str, str]]
) -> None:
    resp = await client.post(FORGOT_URL, json={"email": "nobody@example.com"})
    assert resp.status_code == 202
    assert resp.json() == {"detail": FORGOT_DETAIL}
    assert sent_emails == []


async def test_reset_password_success(
    client: AsyncClient, user: User, sent_emails: list[dict[str, str]]
) -> None:
    old_tokens = await login(client, user.email)
    token = await _request_reset_token(client, sent_emails, user.email)

    resp = await client.post(RESET_URL, json={"token": token, "new_password": NEW_PASSWORD})
    assert resp.status_code == 204, resp.text
    assert resp.content == b""

    # New password works, old one doesn't.
    await login(client, user.email, NEW_PASSWORD)
    resp = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": DEFAULT_PASSWORD}
    )
    assert resp.status_code == 401

    # Refresh tokens issued before the reset are revoked.
    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": old_tokens["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_reset_password_token_is_single_use(
    client: AsyncClient, user: User, sent_emails: list[dict[str, str]]
) -> None:
    token = await _request_reset_token(client, sent_emails, user.email)

    resp = await client.post(RESET_URL, json={"token": token, "new_password": NEW_PASSWORD})
    assert resp.status_code == 204

    resp = await client.post(RESET_URL, json={"token": token, "new_password": "Another-passw0rd"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_reset_token"
    assert isinstance(resp.json()["detail"], str)


async def test_reset_token_dies_when_password_changes(
    client: AsyncClient, user: User, sent_emails: list[dict[str, str]]
) -> None:
    first = await _request_reset_token(client, sent_emails, user.email)
    second = await _request_reset_token(client, sent_emails, user.email)

    resp = await client.post(RESET_URL, json={"token": second, "new_password": NEW_PASSWORD})
    assert resp.status_code == 204

    # The other outstanding link was issued for the old password.
    resp = await client.post(RESET_URL, json={"token": first, "new_password": "Another-passw0rd"})
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_reset_token"


async def test_reset_password_expired_token(client: AsyncClient, user: User) -> None:
    token = create_password_reset_token(
        str(user.id), user.hashed_password, expires_delta=timedelta(seconds=-1)
    )
    resp = await client.post(RESET_URL, json={"token": token, "new_password": NEW_PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_reset_token"


async def test_reset_password_rejects_access_token(client: AsyncClient, user: User) -> None:
    resp = await client.post(
        RESET_URL,
        json={"token": create_access_token(str(user.id)), "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_reset_token"


async def test_reset_password_rejects_garbage_token(client: AsyncClient) -> None:
    resp = await client.post(RESET_URL, json={"token": "not-a-jwt", "new_password": NEW_PASSWORD})
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_reset_token"


async def test_reset_token_cannot_be_used_as_access_token(client: AsyncClient, user: User) -> None:
    token = create_password_reset_token(str(user.id), user.hashed_password)
    resp = await client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


async def test_reset_password_rejects_weak_password(
    client: AsyncClient, user: User, sent_emails: list[dict[str, str]]
) -> None:
    token = await _request_reset_token(client, sent_emails, user.email)
    resp = await client.post(RESET_URL, json={"token": token, "new_password": "short"})
    assert resp.status_code == 422

    # A rejected request doesn't burn the token.
    resp = await client.post(RESET_URL, json={"token": token, "new_password": NEW_PASSWORD})
    assert resp.status_code == 204
