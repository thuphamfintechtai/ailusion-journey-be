import time
import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import (
    TokenPayload,
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import TokenPair
from app.services import user_service

_REVOKED_KEY_PREFIX = "auth:revoked:"


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = await user_service.get_by_email(db, email)
    # verify_password runs even when the user is missing, so timing doesn't leak which emails exist.
    password_ok = verify_password(password, user.hashed_password if user else None)
    if user is None or not password_ok:
        raise UnauthorizedError("Incorrect email or password")
    if not user.is_active:
        raise ForbiddenError("Inactive user")
    return user


def issue_tokens(user: User) -> TokenPair:
    subject = str(user.id)
    return TokenPair(
        access_token=create_access_token(subject),
        refresh_token=create_refresh_token(subject),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def _revoke(redis: Redis, payload: TokenPayload) -> bool:
    """Mark a token's jti as revoked until it expires.

    Returns False if it was already revoked (atomic via SET NX).
    """
    ttl = max(payload.exp - int(time.time()), 1)
    was_set = await redis.set(f"{_REVOKED_KEY_PREFIX}{payload.jti}", "1", ex=ttl, nx=True)
    return bool(was_set)


async def refresh_tokens(db: AsyncSession, redis: Redis, refresh_token: str) -> TokenPair:
    """Exchange a refresh token for a new pair; the old refresh token is revoked (rotation)."""
    payload = decode_token(refresh_token, "refresh")
    if not await _revoke(redis, payload):
        raise UnauthorizedError("Token has been revoked")

    try:
        user_id = uuid.UUID(payload.sub)
    except ValueError as exc:
        raise UnauthorizedError() from exc

    user = await user_service.get_by_id(db, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError()
    return issue_tokens(user)


async def logout(redis: Redis, refresh_token: str) -> None:
    payload = decode_token(refresh_token, "refresh")
    await _revoke(redis, payload)
