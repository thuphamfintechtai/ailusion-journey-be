import hmac
import time
import uuid
from typing import cast

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

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
# Login sessions: session id -> user id, alive from an email + password login until logout,
# password reset, or REFRESH_TOKEN_EXPIRE_DAYS without a refresh. Every access token must
# belong to a live session, so a token that did not come from a login — or outlived its
# logout — is refused at once instead of working until it expires.
_SESSION_KEY_PREFIX = "auth:session:"
_USER_SESSIONS_KEY_PREFIX = "auth:user-sessions:"  # user id -> set of session ids
_SESSION_TTL = settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
SESSION_ENDED_MESSAGE = "Phiên đăng nhập đã kết thúc, vui lòng đăng nhập lại."


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = await user_service.get_by_email(db, email)
    # verify_password runs even when the user is missing, so timing doesn't leak which emails exist.
    password_ok = await run_in_threadpool(
        verify_password, password, user.hashed_password if user else None
    )
    if user is None or not password_ok:
        raise UnauthorizedError("Incorrect email or password")
    if not user.is_active:
        raise ForbiddenError("Inactive user")
    return user


def _issue_tokens(user: User, session_id: str) -> TokenPair:
    subject = str(user.id)
    return TokenPair(
        access_token=create_access_token(subject, session_id),
        refresh_token=create_refresh_token(subject, session_id),
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def start_session(redis: Redis, user: User) -> TokenPair:
    """Open a login session for a user who just proved email + password; return its tokens."""
    session_id = uuid.uuid4().hex
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(f"{_SESSION_KEY_PREFIX}{session_id}", str(user.id), ex=_SESSION_TTL)
        pipe.sadd(f"{_USER_SESSIONS_KEY_PREFIX}{user.id}", session_id)
        pipe.expire(f"{_USER_SESSIONS_KEY_PREFIX}{user.id}", _SESSION_TTL)
        await pipe.execute()
    return _issue_tokens(user, session_id)


async def require_session(redis: Redis, payload: TokenPayload) -> None:
    """Refuse a token whose login session has ended (or never existed)."""
    owner = await redis.get(f"{_SESSION_KEY_PREFIX}{payload.sid}")
    if owner is None or owner != payload.sub:
        raise UnauthorizedError(SESSION_ENDED_MESSAGE)


async def _end_session(redis: Redis, payload: TokenPayload) -> None:
    async with redis.pipeline(transaction=True) as pipe:
        pipe.delete(f"{_SESSION_KEY_PREFIX}{payload.sid}")
        pipe.srem(f"{_USER_SESSIONS_KEY_PREFIX}{payload.sub}", payload.sid)
        await pipe.execute()


async def _end_all_sessions(redis: Redis, user_id: uuid.UUID) -> None:
    key = f"{_USER_SESSIONS_KEY_PREFIX}{user_id}"
    # The client decodes responses (decode_responses=True): members are str.
    session_ids = cast("set[str]", await redis.smembers(key))
    await redis.delete(key, *(f"{_SESSION_KEY_PREFIX}{sid}" for sid in session_ids))


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
    await require_session(redis, payload)
    if not await _revoke(redis, payload):
        raise UnauthorizedError("Token has been revoked")

    user = await get_token_user(db, payload)
    # Same session, kept alive for another refresh period.
    await redis.expire(f"{_SESSION_KEY_PREFIX}{payload.sid}", _SESSION_TTL)
    await redis.expire(f"{_USER_SESSIONS_KEY_PREFIX}{payload.sub}", _SESSION_TTL)
    return _issue_tokens(user, payload.sid)


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
    """End the session: its refresh token and every access token from it stop working now."""
    payload = decode_token(refresh_token, "refresh")
    await _revoke(redis, payload)
    await _end_session(redis, payload)


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
    or the password changes. Every login session (and its tokens) ends with the reset."""
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

    user.hashed_password = await run_in_threadpool(hash_password, new_password)
    await db.commit()

    await _end_all_sessions(redis, user.id)
    await redis.set(
        f"{_PASSWORD_RESET_AT_KEY_PREFIX}{user.id}",
        str(int(time.time())),
        ex=settings.REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60,
    )
