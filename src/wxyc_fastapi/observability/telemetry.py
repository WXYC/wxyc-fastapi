"""Per-request telemetry for FastAPI services.

``RequestTelemetry`` tracks step timings and per-service API-call counters for a
single request. Consumers parameterize it with the API services they care about
(``api_call_keys``), the PostHog ``distinct_id``, and an ``event_prefix`` used
to namespace step events (e.g. ``"lookup"`` produces ``lookup_parse``,
``lookup_search``, ``lookup_completed``).

``send_to_posthog`` emits one PostHog event per tracked step plus a summary
event carrying total duration, step timings, API-call counts, and the cache
stats from :mod:`wxyc_fastapi.observability.cache_stats`.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wxyc_fastapi.observability.cache_stats import get_cache_stats

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

_DEFAULT_CACHE_STATS: dict[str, float] = {
    "memory_hits": 0,
    "pg_hits": 0,
    "pg_misses": 0,
    "api_calls": 0,
    "pg_time_ms": 0.0,
    "api_time_ms": 0.0,
}


@dataclass
class StepResult:
    """Outcome of a single tracked step."""

    duration_ms: float
    success: bool = True
    error_type: str | None = None


@dataclass
class RequestTelemetry:
    """Tracks performance metrics for a single request.

    Args:
        api_call_keys: Names of the upstream services this consumer counts API
            calls for (e.g. ``["groq", "discogs", "slack"]`` for rom,
            ``["discogs"]`` for LML).
        distinct_id: PostHog ``distinct_id`` to attach to every emitted event.
            Conventionally ``"<service-name>-service"``.
        event_prefix: Prefix used to namespace events. ``"lookup"`` produces
            ``lookup_<step>`` per-step events and a ``lookup_completed`` summary.
    """

    api_call_keys: list[str]
    distinct_id: str
    event_prefix: str

    steps: dict[str, StepResult] = field(default_factory=dict)
    api_calls: dict[str, int] = field(init=False)
    start_time: float = field(default_factory=time.perf_counter)

    _current_step: str | None = field(default=None, repr=False)
    _step_start: float = field(default=0.0, repr=False)

    def __post_init__(self) -> None:
        self.api_calls = dict.fromkeys(self.api_call_keys, 0)

    @contextmanager
    def track_step(self, step_name: str):
        """Time a named step. Records ``StepResult`` even on exception (and re-raises)."""
        self._current_step = step_name
        self._step_start = time.perf_counter()
        error_type: str | None = None
        try:
            yield
        except Exception as e:
            error_type = type(e).__name__
            raise
        finally:
            duration_ms = (time.perf_counter() - self._step_start) * 1000
            self.steps[step_name] = StepResult(
                duration_ms=duration_ms,
                success=error_type is None,
                error_type=error_type,
            )
            self._current_step = None

    def record_api_call(self, service: str) -> None:
        """Increment the API-call counter for ``service``.

        Unknown services log a warning and are otherwise ignored, matching the
        existing rom/LML behavior.
        """
        if service in self.api_calls:
            self.api_calls[service] += 1
        else:
            logger.warning("Unknown service for API call tracking: %s", service)

    def get_total_duration_ms(self) -> float:
        """Elapsed wall-clock time since this telemetry was created, in milliseconds."""
        return (time.perf_counter() - self.start_time) * 1000

    def get_step_timings(self) -> dict[str, float]:
        """Step durations as ``{<name>_ms: <duration>}``."""
        return {f"{name}_ms": step.duration_ms for name, step in self.steps.items()}

    def send_to_posthog(
        self,
        posthog_client: Posthog,
        extra_properties: dict[str, Any] | None = None,
    ) -> None:
        """Emit per-step events and a summary event to PostHog.

        Cache stats are read from
        :func:`wxyc_fastapi.observability.cache_stats.get_cache_stats`. When
        unset for the current async context, a zeroed dict of the base keys is
        used so summary events have a stable shape downstream.
        """
        extra_properties = extra_properties or {}

        for step_name, step_result in self.steps.items():
            posthog_client.capture(
                distinct_id=self.distinct_id,
                event=f"{self.event_prefix}_{step_name}",
                properties={
                    "step": step_name,
                    "duration_ms": round(step_result.duration_ms, 2),
                    "success": step_result.success,
                    "error_type": step_result.error_type,
                },
            )

        cache_data = get_cache_stats()
        cache_props = cache_data.copy() if cache_data else _DEFAULT_CACHE_STATS.copy()

        posthog_client.capture(
            distinct_id=self.distinct_id,
            event=f"{self.event_prefix}_completed",
            properties={
                "total_duration_ms": round(self.get_total_duration_ms(), 2),
                "steps": self.get_step_timings(),
                "api_calls": self.api_calls.copy(),
                "cache": cache_props,
                **extra_properties,
            },
        )

        logger.debug(
            "Sent telemetry: %d steps, total %.1fms",
            len(self.steps),
            self.get_total_duration_ms(),
        )
