from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status
from fastapi.security import OAuth2PasswordRequestForm

from app.api.deps import DbSession, RedisClient
from app.schemas.auth import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RefreshRequest,
    ResetPasswordRequest,
    TokenPair,
)
from app.schemas.user import UserCreate, UserRead
from app.services import auth_service, email_service, user_service

FORGOT_PASSWORD_DETAIL = "Nếu email tồn tại, liên kết đặt lại mật khẩu đã được gửi."  # noqa: S105

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
    user = await auth_service.authenticate(db, form.username, form.password)
    return auth_service.issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, db: DbSession, redis: RedisClient) -> TokenPair:
    return await auth_service.refresh_tokens(db, redis, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: RefreshRequest, redis: RedisClient) -> None:
    await auth_service.logout(redis, data.refresh_token)


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def forgot_password(
    data: ForgotPasswordRequest, db: DbSession, background_tasks: BackgroundTasks
) -> ForgotPasswordResponse:
    """Email a password reset link. Same response whether or not the email exists;
    the email is sent after the response so timing doesn't reveal it either."""
    reset = await auth_service.create_password_reset(db, data.email)
    if reset is not None:
        user, token = reset
        background_tasks.add_task(email_service.send_password_reset_email, user.email, token)
    return ForgotPasswordResponse(detail=FORGOT_PASSWORD_DETAIL)


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(data: ResetPasswordRequest, db: DbSession, redis: RedisClient) -> None:
    """Set a new password using the token from the reset email.
    400 `invalid_reset_token` if the token is invalid, expired, or already used."""
    await auth_service.reset_password(db, redis, data.token, data.new_password)
