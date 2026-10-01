from typing import Literal

from pydantic import BaseModel

from app.schemas.common import NormalizedEmail


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
