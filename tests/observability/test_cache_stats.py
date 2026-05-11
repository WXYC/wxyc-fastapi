"""Tests for wxyc_fastapi.observability.cache_stats."""

import asyncio

import pytest

from wxyc_fastapi.observability.cache_stats import (
    CacheStatsRecorder,
    get_cache_stats,
    get_cache_stats_recorder,
    init_cache_stats,
    timed_api,
    timed_pg,
)

BASE_KEYS = {
    "memory_hits",
    "pg_hits",
    "pg_misses",
    "api_calls",
    "pg_time_ms",
    "api_time_ms",
}


class TestInitCacheStats:
    def test_initializes_six_base_keys(self):
        init_cache_stats()
        stats = get_cache_stats()
        assert stats is not None
        assert set(stats.keys()) == BASE_KEYS
        assert all(v == 0 for v in stats.values())

    def test_extra_keys_extend_base_set(self):
        init_cache_stats(extra_keys=["memory_misses", "evictions"])
        stats = get_cache_stats()
        assert stats is not None
        assert "memory_misses" in stats and stats["memory_misses"] == 0
        assert "evictions" in stats and stats["evictions"] == 0
        # base keys still present
        assert BASE_KEYS.issubset(stats.keys())

    def test_get_cache_stats_returns_none_without_init(self):
        # Run in a fresh task context — no init_cache_stats call.
        async def check() -> dict | None:
            return get_cache_stats()

        result = asyncio.run(check())
        assert result is None


class TestRecorder:
    def test_recorder_is_singleton(self):
        a = get_cache_stats_recorder()
        b = get_cache_stats_recorder()
        assert a is b
        assert isinstance(a, CacheStatsRecorder)

    def test_record_memory_cache_hit(self):
        init_cache_stats()
        rec = get_cache_stats_recorder()
        rec.record_memory_cache_hit()
        rec.record_memory_cache_hit()
        assert get_cache_stats()["memory_hits"] == 2

    def test_record_pg_cache_hit_and_miss(self):
        init_cache_stats()
        rec = get_cache_stats_recorder()
        rec.record_pg_cache_hit()
        rec.record_pg_cache_miss()
        rec.record_pg_cache_miss()
        stats = get_cache_stats()
        assert stats["pg_hits"] == 1
        assert stats["pg_misses"] == 2

    def test_record_api_call(self):
        init_cache_stats()
        rec = get_cache_stats_recorder()
        rec.record_api_call()
        rec.record_api_call()
        rec.record_api_call()
        assert get_cache_stats()["api_calls"] == 3

    def test_record_pg_time_accumulates(self):
        init_cache_stats()
        rec = get_cache_stats_recorder()
        rec.record_pg_time(12.5)
        rec.record_pg_time(7.5)
        assert get_cache_stats()["pg_time_ms"] == 20.0

    def test_record_api_time_accumulates(self):
        init_cache_stats()
        rec = get_cache_stats_recorder()
        rec.record_api_time(33.3)
        assert get_cache_stats()["api_time_ms"] == 33.3

    def test_record_extra_key_via_generic_record(self):
        init_cache_stats(extra_keys=["memory_misses"])
        rec = get_cache_stats_recorder()
        rec.record("memory_misses")
        rec.record("memory_misses")
        assert get_cache_stats()["memory_misses"] == 2

    def test_record_memory_cache_miss_method_helper(self):
        # rom calls record_memory_cache_miss(); the method should exist and update
        # the "memory_misses" key when it's been declared via extra_keys.
        init_cache_stats(extra_keys=["memory_misses"])
        rec = get_cache_stats_recorder()
        rec.record_memory_cache_miss()
        assert get_cache_stats()["memory_misses"] == 1

    def test_recorder_methods_are_noop_without_init(self):
        async def check() -> None:
            rec = get_cache_stats_recorder()
            # Should not raise; just no-op silently
            rec.record_memory_cache_hit()
            rec.record_pg_cache_miss()
            rec.record_pg_time(50.0)
            assert get_cache_stats() is None

        asyncio.run(check())

    def test_record_undeclared_key_creates_entry(self):
        # The stats dict is open by design: extra_keys is a "shape guarantee" for
        # downstream consumers reading get_cache_stats() — not a permission gate.
        # Recording an undeclared key adds it on first call.
        init_cache_stats()
        rec = get_cache_stats_recorder()
        rec.record("unexpected", 5)
        assert get_cache_stats()["unexpected"] == 5


@pytest.mark.asyncio
class TestTimedContextManagers:
    async def test_timed_pg_records_duration(self):
        init_cache_stats()
        async with timed_pg():
            await asyncio.sleep(0.01)
        assert get_cache_stats()["pg_time_ms"] >= 9  # ~10ms, allow scheduler slack

    async def test_timed_api_records_duration(self):
        init_cache_stats()
        async with timed_api():
            await asyncio.sleep(0.01)
        assert get_cache_stats()["api_time_ms"] >= 9

    async def test_timed_pg_records_even_on_exception(self):
        init_cache_stats()
        with pytest.raises(RuntimeError):
            async with timed_pg():
                await asyncio.sleep(0.005)
                raise RuntimeError("boom")
        assert get_cache_stats()["pg_time_ms"] > 0


class TestContextIsolation:
    def test_init_in_one_task_does_not_leak_to_another(self):
        # Different asyncio tasks get different ContextVar copies.
        async def task_a():
            init_cache_stats()
            await asyncio.sleep(0)
            get_cache_stats_recorder().record_memory_cache_hit()
            return get_cache_stats()["memory_hits"]

        async def task_b():
            await asyncio.sleep(0)
            return get_cache_stats()  # never initialized in this task

        async def main():
            a, b = await asyncio.gather(task_a(), task_b())
            return a, b

        a_hits, b_stats = asyncio.run(main())
        assert a_hits == 1
        assert b_stats is None
