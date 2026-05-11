"""Per-request cache statistics recorder backed by a ContextVar.

Each request initializes its own stats dict via ``init_cache_stats()``; recorder
methods then update that dict. When the dict has not been initialized for the
current async context, recorder methods are silent no-ops so library code can
call them unconditionally without worrying about whether telemetry is wired.

The base set covers the metrics every WXYC FastAPI service tracks:
``memory_hits``, ``pg_hits``, ``pg_misses``, ``api_calls``, ``pg_time_ms``,
``api_time_ms``. Consumers can extend the set via ``extra_keys`` (rom adds
``memory_misses``).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar

_cache_stats_var: ContextVar[dict[str, float]] = ContextVar("wxyc_fastapi_cache_stats")

_BASE_KEYS: tuple[str, ...] = (
    "memory_hits",
    "pg_hits",
    "pg_misses",
    "api_calls",
    "pg_time_ms",
    "api_time_ms",
)


def init_cache_stats(extra_keys: Iterable[str] | None = None) -> None:
    """Initialize the cache-stats dict for the current request context.

    Args:
        extra_keys: Additional keys to seed in the stats dict (initial value 0).
            Use this when a consumer tracks metrics beyond the base set
            (e.g., rom tracks ``memory_misses``).
    """
    stats: dict[str, float] = dict.fromkeys(_BASE_KEYS, 0)
    if extra_keys:
        for key in extra_keys:
            stats.setdefault(key, 0)
    _cache_stats_var.set(stats)


def get_cache_stats() -> dict[str, float] | None:
    """Return the current request's cache-stats dict, or ``None`` if uninitialized."""
    return _cache_stats_var.get(None)


class CacheStatsRecorder:
    """Stateless recorder that updates the per-request cache-stats dict.

    Recorder methods are no-ops when ``init_cache_stats()`` has not been called
    for the current async context, so consumer code can call them unconditionally.

    Use ``get_cache_stats_recorder()`` to obtain the singleton instance.
    """

    def record(self, key: str, value: float = 1) -> None:
        """Add ``value`` to ``stats[key]``. Silent no-op when stats are uninitialized.

        Use this for keys declared via ``extra_keys`` or for ad-hoc metrics that
        do not have a dedicated helper method.
        """
        stats = _cache_stats_var.get(None)
        if stats is None:
            return
        stats[key] = stats.get(key, 0) + value

    def record_memory_cache_hit(self) -> None:
        self.record("memory_hits")

    def record_memory_cache_miss(self) -> None:
        self.record("memory_misses")

    def record_pg_cache_hit(self) -> None:
        self.record("pg_hits")

    def record_pg_cache_miss(self) -> None:
        self.record("pg_misses")

    def record_api_call(self) -> None:
        self.record("api_calls")

    def record_pg_time(self, ms: float) -> None:
        self.record("pg_time_ms", ms)

    def record_api_time(self, ms: float) -> None:
        self.record("api_time_ms", ms)


_recorder = CacheStatsRecorder()


def get_cache_stats_recorder() -> CacheStatsRecorder:
    """Return the singleton ``CacheStatsRecorder`` instance."""
    return _recorder


@asynccontextmanager
async def timed_pg():
    """Async context manager that accumulates elapsed time into ``pg_time_ms``.

    Records duration even if the body raises.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        _recorder.record_pg_time((time.perf_counter() - start) * 1000)


@asynccontextmanager
async def timed_api():
    """Async context manager that accumulates elapsed time into ``api_time_ms``.

    Records duration even if the body raises.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        _recorder.record_api_time((time.perf_counter() - start) * 1000)
