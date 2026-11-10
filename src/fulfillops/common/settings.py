"""All settings come from environment variables and are read here."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(RuntimeError):
    """A setting is missing or has a bad value."""


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    database_url: str  # secret, never log it
    pool_size: int  # max connections per process
    port: int
    app_env: str  # production (default), development or test
    fail_readiness: bool  # test-only: makes /health/ready return 503
    migrations_dir: Path

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    database_url = env.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigError("DATABASE_URL is required")

    app_env = env.get("APP_ENV", "production").strip().lower() or "production"
    if app_env not in {"production", "development", "test"}:
        raise ConfigError("APP_ENV must be production, development or test")

    return Settings(
        database_url=database_url,
        pool_size=_int(env, "POOL_SIZE", 20),
        port=_int(env, "PORT", 8080),
        app_env=app_env,
        fail_readiness=_bool(env, "FAIL_READINESS", False),
        migrations_dir=Path(env.get("MIGRATIONS_DIR", "db/migrations")),
    )
