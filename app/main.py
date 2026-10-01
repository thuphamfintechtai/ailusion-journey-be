import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.llm import create_llm_client
from app.core.logging import setup_logging
from app.core.middleware import RequestContextMiddleware
from app.core.redis import create_redis
from app.db.session import engine

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = create_redis()
    app.state.llm = create_llm_client()
    logger.info(
        "%s v%s started (env=%s)", settings.PROJECT_NAME, settings.VERSION, settings.ENVIRONMENT
    )
    try:
        yield
    finally:
        await app.state.llm.aclose()
        await app.state.redis.aclose()
        await engine.dispose()
        logger.info("Shutdown complete")


def create_app() -> FastAPI:
    setup_logging(settings.LOG_LEVEL)

    docs_enabled = not settings.is_production
    app = FastAPI(
        title=settings.PROJECT_NAME,
        version=settings.VERSION,
        lifespan=lifespan,
        openapi_url=f"{settings.API_V1_PREFIX}/openapi.json" if docs_enabled else None,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
    )

    app.add_middleware(RequestContextMiddleware)
    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-ID"],
        )

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
