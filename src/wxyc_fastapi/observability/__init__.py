"""Observability primitives for WXYC FastAPI services.

Phase A is split across three PRs; symbols become available as each lands:

- PR 1: ``cache_stats``
- PR 2 (this commit): ``sentry``, ``posthog``
- PR 3: ``telemetry``
"""

from wxyc_fastapi.observability.cache_stats import (
    CacheStatsRecorder,
    get_cache_stats,
    get_cache_stats_recorder,
    init_cache_stats,
    timed_api,
    timed_pg,
)
from wxyc_fastapi.observability.posthog import (
    flush_posthog,
    get_posthog_client,
    shutdown_posthog,
)
from wxyc_fastapi.observability.sentry import (
    add_breadcrumb,
    capture_exception,
    init_sentry,
)

__all__ = [
    "CacheStatsRecorder",
    "add_breadcrumb",
    "capture_exception",
    "flush_posthog",
    "get_cache_stats",
    "get_cache_stats_recorder",
    "get_posthog_client",
    "init_cache_stats",
    "init_sentry",
    "shutdown_posthog",
    "timed_api",
    "timed_pg",
]
