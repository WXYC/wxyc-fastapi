"""Observability primitives: Sentry init, request telemetry, cache-stats recorder, PostHog wrapper."""

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
from wxyc_fastapi.observability.telemetry import (
    RequestTelemetry,
    StepResult,
)

__all__ = [
    "CacheStatsRecorder",
    "RequestTelemetry",
    "StepResult",
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
