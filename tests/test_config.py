import pytest

from app.core.config import Settings


def test_database_url_carries_sslmode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_SSLMODE", "require")
    url = Settings().database_url
    assert url.query["ssl"] == "require"


def test_database_url_sslmode_defaults_to_prefer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PG_SSLMODE", raising=False)
    monkeypatch.delenv("POSTGRES_SSLMODE", raising=False)
    assert Settings(_env_file=None).database_url.query["ssl"] == "prefer"


def test_unknown_sslmode_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PG_SSLMODE", "yes")
    with pytest.raises(ValueError):
        Settings()
