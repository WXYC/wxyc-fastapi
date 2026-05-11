"""Observability primitives for WXYC FastAPI services.

Phase A is split across three PRs; symbols become available as each lands:

- PR 1 (this commit): ``cache_stats``
- PR 2: ``sentry``, ``posthog``
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

__all__ = [
    "CacheStatsRecorder",
    "get_cache_stats",
    "get_cache_stats_recorder",
    "init_cache_stats",
    "timed_api",
    "timed_pg",
]
