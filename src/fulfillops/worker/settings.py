"""Worker settings: everything from environment variables, all with defaults.

Only DATABASE_URL is required (it comes from common.settings, same secret as the API).
"""

from __future__ import annotations

import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass

from fulfillops.common.settings import ConfigError, Settings, int_setting, load_settings


def _float(env: Mapping[str, str], name: str, default: float, minimum: float = 0.0) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class WorkerSettings:
    base: Settings  # database_url, app_env, ...
    pool_size: int  # connections for this worker (it handles one job at a time)
    worker_id: str  # written to jobs.locked_by
    poll_interval_s: float  # wait when no job is due
    lease_seconds: float  # how long a claim is valid (ADR-001 D7: 10 s)
    backoff_base_s: float  # delay after attempt 1; doubles each attempt (2, 4, 8, 16 s)
    backoff_cap_s: float  # no delay is longer than this (30 s)
    processing_seconds: float  # the simulated work (kill-drill: raise it to ~5)
    poison_customer_id: str | None  # always fails; ignored in production


def load_worker_settings(env: Mapping[str, str] | None = None) -> WorkerSettings:
    env = os.environ if env is None else env
    base = load_settings(env)
    poison = env.get("POISON_CUSTOMER_ID", "").strip() or None
    if poison and base.app_env == "production":
        poison = None  # never honoured in production, so it can't be a back door
    worker_id = env.get("WORKER_ID", "").strip() or f"{socket.gethostname()}-{os.getpid()}"
    return WorkerSettings(
        base=base,
        pool_size=int_setting(env, "WORKER_POOL_SIZE", 3),
        worker_id=worker_id,
        poll_interval_s=_float(env, "POLL_INTERVAL_S", 0.5, 0.05),
        lease_seconds=_float(env, "LEASE_SECONDS", 10.0, 1.0),
        backoff_base_s=_float(env, "BACKOFF_BASE_S", 2.0, 0.0),
        backoff_cap_s=_float(env, "BACKOFF_CAP_S", 30.0, 0.0),
        processing_seconds=_float(env, "PROCESSING_SECONDS", 0.5, 0.0),
        poison_customer_id=poison,
    )
