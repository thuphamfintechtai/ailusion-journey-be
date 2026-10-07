from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.DB_ECHO,
    # Pre-ping costs one round trip per checkout; recycling old connections covers most
    # stale-connection cases on its own. Turn pre-ping on if the DB/proxy drops idle links.
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
    connect_args={
        "server_settings": {
            "application_name": settings.PROJECT_NAME,
            # JIT compile time dwarfs the run time of short OLTP queries like ours.
            "jit": "off",
        },
    },
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Services commit explicitly."""
    async with SessionLocal() as session:
        yield session
