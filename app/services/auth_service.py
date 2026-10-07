import hmac
import time
import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import (
    TokenPayload,
    create_access_token,
    create_password_reset_token,
    create_refresh_token,
    decode_password_reset_token,
    decode_token,
    hash_password,
    invalid_reset_token_error,
    password_fingerprint,
    verify_password,
)
from app.models.user import User
from app.schemas.auth import TokenPair
from app.services import user_service

_REVOKED_KEY_PREFIX = "auth:revoked:"
_RESET_USED_KEY_PREFIX = "auth:reset-used:"
# user id -> unix time of the last password reset; refresh tokens issued up to then are rejected.
_PASSWORD_RESET_AT_KEY_PREFIX = "auth:password-reset-at:"  # noqa: S105


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
    if await _issued_before_password_reset(redis, payload):
        raise UnauthorizedError("Token has been revoked")
    if not await _revoke(redis, payload):
        raise UnauthorizedError("Token has been revoked")

    return issue_tokens(await get_token_user(db, payload))


async def get_token_user(db: AsyncSession, payload: TokenPayload) -> User:
    """The active user a token was issued to."""
    try:
        user_id = uuid.UUID(payload.sub)
    except ValueError as exc:
        raise UnauthorizedError() from exc

    user = await user_service.get_by_id(db, user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError()
    return user


async def logout(redis: Redis, refresh_token: str) -> None:
    payload = decode_token(refresh_token, "refresh")
    await _revoke(redis, payload)


async def _issued_before_password_reset(redis: Redis, payload: TokenPayload) -> bool:
    reset_at = await redis.get(f"{_PASSWORD_RESET_AT_KEY_PREFIX}{payload.sub}")
    # iat has 1s resolution: a token from the same second as the reset counts as older.
    return reset_at is not None and payload.iat <= int(reset_at)


async def create_password_reset(db: AsyncSession, email: str) -> tuple[User, str] | None:
    """A reset token for an active user with this email, or None (caller must not reveal which)."""
    user = await user_service.get_by_email(db, email)
    if user is None or not user.is_active:
        return None
    return user, create_password_reset_token(str(user.id), user.hashed_password)


async def reset_password(db: AsyncSession, redis: Redis, token: str, new_password: str) -> None:
    """Set a new password from a reset token. Each token works once, and only until it expires
    or the password changes. All refresh tokens issued before the reset stop working."""
    payload = decode_password_reset_token(token)
    try:
        user_id = uuid.UUID(payload.sub)
    except ValueError as exc:
        raise invalid_reset_token_error() from exc

    user = await user_service.get_by_id(db, user_id)
    if (
        user is None
        or not user.is_active
        or not hmac.compare_digest(payload.pwd, password_fingerprint(user.hashed_password))
    ):
        raise invalid_reset_token_error()

    # Single use: claim the jti atomically before changing anything.
    ttl = max(payload.exp - int(time.time()), 1)
    if not await redis.set(f"{_RESET_USED_KEY_PREFIX}{payload.jti}", "1", ex=ttl, nx=True):
        raise invalid_reset_token_error()

    user.hashed_password = hash_password(new_password)
    await db.commit()

    await redis.set(
        f"{_PASSWORD_RESET_AT_KEY_PREFIX}{user.id}",
        str(int(time.time())),
        ex=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )
