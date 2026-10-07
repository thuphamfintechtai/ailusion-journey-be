import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.exceptions import ConflictError
from app.core.security import hash_password
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


async def get_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def get_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email == email.lower()))


async def create_user(db: AsyncSession, data: UserCreate, *, is_superuser: bool = False) -> User:
    if await get_by_email(db, data.email) is not None:
        raise ConflictError("Email already registered")

    user = User(
        email=data.email,
        hashed_password=await run_in_threadpool(hash_password, data.password),
        full_name=data.full_name,
        is_superuser=is_superuser,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:  # concurrent registration with the same email
        await db.rollback()
        raise ConflictError("Email already registered") from exc
    return user


async def update_user(db: AsyncSession, user: User, data: UserUpdate) -> User:
    values = data.model_dump(exclude_unset=True)
    password = values.pop("password", None)
    if password:
        user.hashed_password = await run_in_threadpool(hash_password, password)
    for field, value in values.items():
        setattr(user, field, value)
    await db.commit()
    return user


async def list_users(db: AsyncSession, *, limit: int, offset: int) -> tuple[list[User], int]:
    total = await db.scalar(select(func.count()).select_from(User)) or 0
    result = await db.scalars(
        select(User).order_by(User.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.all()), total
