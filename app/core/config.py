import json
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

_INSECURE_SECRET_KEY = "insecure-dev-secret-key-change-me-in-production"  # noqa: S105


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    PROJECT_NAME: str = "Ailusion Journey API"
    VERSION: str = "0.1.0"
    ENVIRONMENT: Literal["local", "staging", "production"] = "local"
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    # Comma-separated list or JSON array, e.g. "http://localhost:3000,https://app.example.com"
    CORS_ORIGINS: Annotated[list[str], NoDecode] = []

    # --- Security / JWT ---
    SECRET_KEY: str = _INSECURE_SECRET_KEY
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # --- Frontend ---
    # Used to build links in emails, e.g. {FRONTEND_URL}/reset-password?token=...
    FRONTEND_URL: str = "http://localhost:3000"

    # --- Email (SMTP) ---
    # Leave SMTP_HOST empty in dev: emails are logged instead of sent.
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "no-reply@ailusion.local"
    # STARTTLS after connecting (port 587). Set false for plain SMTP (e.g. a local Mailpit).
    SMTP_TLS: bool = True

    # --- PostgreSQL ---
    # Each also reads the PG_* name (PG_HOST, PG_PORT, PG_USER, PG_PASSWORD, PG_DATABASE).
    POSTGRES_HOST: str = Field(
        default="localhost", validation_alias=AliasChoices("POSTGRES_HOST", "PG_HOST")
    )
    POSTGRES_PORT: int = Field(
        default=5432, validation_alias=AliasChoices("POSTGRES_PORT", "PG_PORT")
    )
    POSTGRES_USER: str = Field(
        default="postgres", validation_alias=AliasChoices("POSTGRES_USER", "PG_USER")
    )
    POSTGRES_PASSWORD: str = Field(
        default="postgres",  # local dev default
        validation_alias=AliasChoices("POSTGRES_PASSWORD", "PG_PASSWORD"),
    )
    POSTGRES_DB: str = Field(
        default="ailusion_journey", validation_alias=AliasChoices("POSTGRES_DB", "PG_DATABASE")
    )
    # TLS to Postgres (asyncpg). "prefer" works with a server that has SSL off (local Docker)
    # and encrypts as soon as the server turns it on; use "require" (or "verify-full" with a
    # CA-signed certificate) for any database reached over the internet.
    POSTGRES_SSLMODE: Literal[
        "disable", "allow", "prefer", "require", "verify-ca", "verify-full"
    ] = Field(default="prefer", validation_alias=AliasChoices("POSTGRES_SSLMODE", "PG_SSLMODE"))
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_PRE_PING: bool = False
    DB_POOL_RECYCLE_SECONDS: int = 1800
    # Fail fast instead of queueing a request for 30s when the pool is exhausted.
    DB_POOL_TIMEOUT_SECONDS: float = 10.0

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- LLM service (ailusion-journey-llm) ---
    LLM_SERVICE_URL: str = "http://localhost:8001"
    # Generous: one answer can take a while to generate.
    LLM_SERVICE_TIMEOUT: float = 120.0
    # How long a conversation stays listed. Match CHECKPOINT_TTL_MINUTES in the LLM service.
    CHAT_THREAD_TTL_MINUTES: int = 1440
    # A trip plan runs ~15 tool calls and 2 LLM calls upstream: give it longer than one chat turn.
    LLM_PLAN_TIMEOUT: float = 300.0
    # How long a plan stays reachable (its progress events live as long as the checkpoint).
    PLAN_THREAD_TTL_MINUTES: int = 1440

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if value.startswith("["):
                return json.loads(value)
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @model_validator(mode="after")
    def _check_secret_key(self) -> Self:
        if self.ENVIRONMENT != "local" and self.SECRET_KEY == _INSECURE_SECRET_KEY:
            raise ValueError("SECRET_KEY must be set when ENVIRONMENT is not 'local'")
        if len(self.SECRET_KEY) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters")
        return self

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            database=self.POSTGRES_DB,
            query={"ssl": self.POSTGRES_SSLMODE},
        )

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
