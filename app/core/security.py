import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt
from pwdlib import PasswordHash
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.exceptions import BadRequestError, UnauthorizedError

TokenType = Literal["access", "refresh"]

# Argon2id with recommended parameters.
password_hasher = PasswordHash.recommended()

# Used to keep login timing constant when the email does not exist.
_DUMMY_HASH = password_hasher.hash("dummy-password-for-timing")


class TokenPayload(BaseModel):
    sub: str
    type: TokenType
    jti: str
    iat: int
    exp: int


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(plain_password: str, hashed_password: str | None) -> bool:
    if hashed_password is None:
        password_hasher.verify(plain_password, _DUMMY_HASH)
        return False
    return password_hasher.verify(plain_password, hashed_password)


def _create_token(subject: str, token_type: TokenType, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": subject,
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str) -> str:
    return _create_token(subject, "access", timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(subject: str) -> str:
    return _create_token(subject, "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS))


def decode_token(token: str, expected_type: TokenType) -> TokenPayload:
    try:
        raw = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "type", "jti", "iat", "exp"]},
        )
        payload = TokenPayload.model_validate(raw)
    except (jwt.InvalidTokenError, ValidationError) as exc:
        raise UnauthorizedError("Invalid or expired token") from exc

    if payload.type != expected_type:
        raise UnauthorizedError("Invalid token type")
    return payload


# --- Password reset tokens ---
# A separate token type: never accepted where an access/refresh token is expected, and vice versa.

INVALID_RESET_TOKEN_CODE = "invalid_reset_token"  # noqa: S105
INVALID_RESET_LINK_MESSAGE = "Liên kết đặt lại mật khẩu không hợp lệ hoặc đã hết hạn."


class PasswordResetTokenPayload(BaseModel):
    sub: str
    type: Literal["reset"]
    jti: str
    iat: int
    exp: int
    # Fingerprint of the password hash at issue time: the token dies once the password changes.
    pwd: str


def password_fingerprint(hashed_password: str) -> str:
    digest = hmac.new(
        settings.SECRET_KEY.encode(), hashed_password.encode(), hashlib.sha256
    ).hexdigest()
    return digest[:32]


def create_password_reset_token(
    subject: str, hashed_password: str, expires_delta: timedelta | None = None
) -> str:
    now = datetime.now(UTC)
    if expires_delta is None:
        expires_delta = timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": subject,
        "type": "reset",
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + expires_delta,
        "pwd": password_fingerprint(hashed_password),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def invalid_reset_token_error() -> BadRequestError:
    return BadRequestError(INVALID_RESET_LINK_MESSAGE, code=INVALID_RESET_TOKEN_CODE)


def decode_password_reset_token(token: str) -> PasswordResetTokenPayload:
    try:
        raw = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "type", "jti", "iat", "exp"]},
        )
        return PasswordResetTokenPayload.model_validate(raw)
    except (jwt.InvalidTokenError, ValidationError) as exc:
        raise invalid_reset_token_error() from exc
