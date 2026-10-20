"""Observability primitives: PostHog error tracking, request telemetry, cache-stats recorder, PostHog wrapper."""

from wxyc_fastapi.observability.cache_stats import (
    CacheStatsRecorder,
    get_cache_stats,
    get_cache_stats_recorder,
    init_cache_stats,
    timed_api,
    timed_pg,
)
from wxyc_fastapi.observability.error_tracking import init_error_tracking
from wxyc_fastapi.observability.errors import (
    PosthogErrorsClient,
    PosthogErrorsHandler,
    add_breadcrumb,
    capture_exception,
    get_posthog_errors_client,
    shutdown_posthog_errors,
)
from wxyc_fastapi.observability.posthog import (
    flush_posthog,
    get_posthog_client,
    shutdown_posthog,
)
from wxyc_fastapi.observability.telemetry import (
    RequestTelemetry,
    StepResult,
)

__all__ = [
    "CacheStatsRecorder",
    "PosthogErrorsClient",
    "PosthogErrorsHandler",
    "RequestTelemetry",
    "StepResult",
    "add_breadcrumb",
    "capture_exception",
    "flush_posthog",
    "get_cache_stats",
    "get_cache_stats_recorder",
    "get_posthog_client",
    "get_posthog_errors_client",
    "init_cache_stats",
    "init_error_tracking",
    "shutdown_posthog",
    "shutdown_posthog_errors",
    "timed_api",
    "timed_pg",
]
