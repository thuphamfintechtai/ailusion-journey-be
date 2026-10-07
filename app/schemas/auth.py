from typing import Literal

from pydantic import BaseModel

from app.schemas.common import NormalizedEmail, Password


class LoginRequest(BaseModel):
    email: NormalizedEmail
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int  # access token lifetime, seconds


class ForgotPasswordRequest(BaseModel):
    email: NormalizedEmail


class ForgotPasswordResponse(BaseModel):
    detail: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: Password
