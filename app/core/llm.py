"""HTTP client to the LLM service (`ailusion-journey-llm`).

One shared client for the whole process, created in the app lifespan — the connection pool
is reused across requests instead of opening a new connection per chat message.
"""

import httpx
from fastapi import Request

from app.core.config import settings


def create_llm_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.LLM_SERVICE_URL,
        timeout=httpx.Timeout(settings.LLM_SERVICE_TIMEOUT, connect=5.0),
    )


def get_llm_client(request: Request) -> httpx.AsyncClient:
    """FastAPI dependency: the client created in the app lifespan."""
    client: httpx.AsyncClient = request.app.state.llm
    return client
