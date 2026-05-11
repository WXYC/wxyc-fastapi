"""Test-suite fixtures."""

import contextvars

import pytest


@pytest.fixture(autouse=True)
def _reset_cache_stats_var(monkeypatch):
    """Each test starts with cache_stats ContextVar in its default (unset) state.

    pytest reuses the same Python execution context across tests, so a synchronous
    ``_cache_stats_var.set(...)`` in one test would persist into the next — and
    even into ``asyncio.run()`` blocks, which inherit the parent context. Replacing
    the module-level ContextVar with a fresh instance per test is the cleanest
    way to guarantee isolation.
    """
    from wxyc_fastapi.observability import cache_stats

    fresh = contextvars.ContextVar("wxyc_fastapi_cache_stats")
    monkeypatch.setattr(cache_stats, "_cache_stats_var", fresh)
