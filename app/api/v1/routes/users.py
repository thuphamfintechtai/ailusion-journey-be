import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentSuperuser, CurrentUser, DbSession
from app.core.exceptions import NotFoundError
from app.schemas.common import Page
from app.schemas.user import UserRead, UserUpdate
from app.services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
async def read_me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.patch("/me", response_model=UserRead)
async def update_me(data: UserUpdate, user: CurrentUser, db: DbSession) -> UserRead:
    updated = await user_service.update_user(db, user, data)
    return UserRead.model_validate(updated)


@router.get("", response_model=Page[UserRead])
async def list_users(
    _: CurrentSuperuser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[UserRead]:
    users, total = await user_service.list_users(db, limit=limit, offset=offset)
    return Page(
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}", response_model=UserRead)
async def get_user(user_id: uuid.UUID, _: CurrentSuperuser, db: DbSession) -> UserRead:
    user = await user_service.get_by_id(db, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return UserRead.model_validate(user)
