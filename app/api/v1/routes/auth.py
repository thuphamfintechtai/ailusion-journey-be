from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import DbSession, RedisClient
from app.schemas.auth import LoginRequest, RefreshRequest, TokenPair
from app.schemas.user import UserCreate, UserRead
from app.services import auth_service, user_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate, db: DbSession) -> UserRead:
    user = await user_service.create_user(db, data)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(data: LoginRequest, db: DbSession) -> TokenPair:
    """Log in with a JSON body. Use this from the frontend."""
    user = await auth_service.authenticate(db, data.email, data.password)
    return auth_service.issue_tokens(user)


@router.post("/token", response_model=TokenPair)
async def login_oauth2_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession
) -> TokenPair:
    """OAuth2 password flow (form data, `username` = email). Used by Swagger's Authorize button."""
    user = await auth_service.authenticate(db, form.username.lower(), form.password)
    return auth_service.issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, db: DbSession, redis: RedisClient) -> TokenPair:
    return await auth_service.refresh_tokens(db, redis, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest, redis: RedisClient) -> None:
    await auth_service.logout(redis, data.refresh_token)
