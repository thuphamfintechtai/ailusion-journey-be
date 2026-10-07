from typing import Annotated

import httpx
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ForbiddenError
from app.core.llm import get_llm_client
from app.core.redis import get_redis
from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User
from app.services import auth_service

# tokenUrl powers the "Authorize" button in Swagger UI (/docs).
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_PREFIX}/auth/token")

DbSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[Redis, Depends(get_redis)]
LlmClient = Annotated[httpx.AsyncClient, Depends(get_llm_client)]
Token = Annotated[str, Depends(oauth2_scheme)]


async def get_current_user(db: DbSession, token: Token) -> User:
    user = await auth_service.get_token_user(db, decode_token(token, "access"))
    # End the read transaction so the connection goes back to the pool now, not when the
    # request finishes (chat requests wait on the LLM for up to minutes). expire_on_commit
    # is off, so `user` stays loaded and attached; a later write starts a new transaction.
    await db.commit()
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_superuser(user: CurrentUser) -> User:
    if not user.is_superuser:
        raise ForbiddenError()
    return user


CurrentSuperuser = Annotated[User, Depends(get_current_superuser)]
