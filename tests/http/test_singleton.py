"""Tests for ``wxyc_fastapi.http.singleton.async_singleton``.

The double-check-lock helper exists to prevent the file-descriptor leak
documented in [LML#241](https://github.com/WXYC/library-metadata-lookup/issues/241)
(fix in [LML#242](https://github.com/WXYC/library-metadata-lookup/pull/242)):
without the lock, concurrent first-callers each pass the ``is None`` check,
each construct a new client/pool, and only one survives — the rest are
orphaned with their connections (and FDs) still open.

The headline test in this module — ``test_factory_invoked_once_under_concurrency`` —
is the LML#242 reproducer. Removing the lock from ``async_singleton`` must
fail it.
"""

from __future__ import annotations

import asyncio

import pytest

from wxyc_fastapi.http.singleton import async_singleton


class _Sentinel:
    """Distinguishable per-instance object so identity assertions are unambiguous."""

    def __init__(self) -> None:
        self.closed = False


# ---------------------------------------------------------------------------
# Concurrency invariant — the bug this whole module exists to prevent
# ---------------------------------------------------------------------------


class TestConcurrentFirstCallers:
    @pytest.mark.asyncio
    async def test_factory_invoked_once_under_concurrency(self):
        """LML#241 reproducer: 50 concurrent first-callers must see one factory call.

        The factory yields control via ``asyncio.sleep(0)`` mid-flight so the event
        loop schedules the other waiting callers. Without the lock, those callers
        also pass the ``is None`` check and each invoke the factory; with the lock
        + inner re-check, only the first caller does.
        """
        invocations = 0

        async def factory() -> _Sentinel:
            nonlocal invocations
            invocations += 1
            await asyncio.sleep(0)  # yield, let other tasks run
            return _Sentinel()

        getter, _ = async_singleton(factory)
        results = await asyncio.gather(*(getter() for _ in range(50)))

        assert invocations == 1
        assert all(r is results[0] for r in results)

    @pytest.mark.asyncio
    async def test_returns_same_instance_after_first_call(self):
        """Sequential calls after the first should fast-path back the same object."""
        invocations = 0

        async def factory() -> _Sentinel:
            nonlocal invocations
            invocations += 1
            return _Sentinel()

        getter, _ = async_singleton(factory)
        first = await getter()
        second = await getter()
        third = await getter()

        assert first is second is third
        assert invocations == 1


# ---------------------------------------------------------------------------
# Closer behavior
# ---------------------------------------------------------------------------


class TestCloser:
    @pytest.mark.asyncio
    async def test_close_is_noop_when_uninitialized(self):
        """Calling closer before getter must not raise (e.g. shutdown after a startup that never used the singleton)."""
        called = False

        async def factory() -> _Sentinel:
            nonlocal called
            called = True
            return _Sentinel()

        _, closer = async_singleton(factory)
        await closer()  # must not raise
        assert called is False

    @pytest.mark.asyncio
    async def test_close_resets_so_next_getter_recreates(self):
        """After closer(), getter() should produce a fresh instance via the factory."""
        invocations = 0

        async def factory() -> _Sentinel:
            nonlocal invocations
            invocations += 1
            return _Sentinel()

        getter, closer = async_singleton(factory)
        first = await getter()
        await closer()
        second = await getter()

        assert first is not second
        assert invocations == 2

    @pytest.mark.asyncio
    async def test_close_calls_aclose_when_available(self):
        """If the instance exposes ``aclose`` (httpx convention), use it."""

        class WithAclose:
            def __init__(self) -> None:
                self.aclose_called = False

            async def aclose(self) -> None:
                self.aclose_called = True

        instance = WithAclose()

        async def factory() -> WithAclose:
            return instance

        getter, closer = async_singleton(factory)
        await getter()
        await closer()

        assert instance.aclose_called is True

    @pytest.mark.asyncio
    async def test_close_calls_close_when_only_close_available(self):
        """If the instance has only ``close`` (sync), call it."""

        class WithSyncClose:
            def __init__(self) -> None:
                self.close_called = False

            def close(self) -> None:
                self.close_called = True

        instance = WithSyncClose()

        async def factory() -> WithSyncClose:
            return instance

        getter, closer = async_singleton(factory)
        await getter()
        await closer()

        assert instance.close_called is True

    @pytest.mark.asyncio
    async def test_close_awaits_async_close_when_close_returns_coroutine(self):
        """``asyncpg.Pool.close()`` is async (no ``aclose``); the closer must await it.

        Without this, the cleanup coroutine is created and immediately dropped,
        leaking the underlying connections — defeating the entire point of the helper.
        """

        class AsyncpgPoolLike:
            """Mimics asyncpg.Pool: only ``close``, and it's async."""

            def __init__(self) -> None:
                self.close_awaited = False

            async def close(self) -> None:
                self.close_awaited = True

        instance = AsyncpgPoolLike()

        async def factory() -> AsyncpgPoolLike:
            return instance

        getter, closer = async_singleton(factory)
        await getter()
        await closer()

        assert instance.close_awaited is True

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        """Closing twice should be safe — second call is a no-op, not a crash."""

        class WithAclose:
            def __init__(self) -> None:
                self.aclose_count = 0

            async def aclose(self) -> None:
                self.aclose_count += 1

        instance = WithAclose()

        async def factory() -> WithAclose:
            return instance

        getter, closer = async_singleton(factory)
        await getter()
        await closer()
        await closer()  # must not raise; must not re-call cleanup

        assert instance.aclose_count == 1


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFactoryFailure:
    @pytest.mark.asyncio
    async def test_factory_exception_leaves_instance_unset_so_next_call_retries(self):
        """If the factory raises, the singleton must stay uninitialized.

        Otherwise a single transient init failure would permanently wedge the
        service into returning ``None`` (or worse, a half-built object).
        """
        attempt = 0

        async def factory() -> _Sentinel:
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise RuntimeError("transient init failure")
            return _Sentinel()

        getter, _ = async_singleton(factory)

        with pytest.raises(RuntimeError, match="transient init failure"):
            await getter()

        # Second call must succeed, not return None or re-raise.
        instance = await getter()
        assert isinstance(instance, _Sentinel)
        assert attempt == 2

    @pytest.mark.asyncio
    async def test_factory_exception_releases_lock(self):
        """Factory raising must not deadlock subsequent callers.

        If the lock is held when factory raises, subsequent ``await getter()``
        calls would hang forever. ``async with lock`` releases on exception, so
        this should already be true — the test pins the contract.
        """
        attempts = 0

        async def factory() -> _Sentinel:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise RuntimeError("flaky")
            return _Sentinel()

        getter, _ = async_singleton(factory)

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await getter()
        # Third call should acquire the lock fine and succeed.
        instance = await asyncio.wait_for(getter(), timeout=1.0)
        assert isinstance(instance, _Sentinel)


# ---------------------------------------------------------------------------
# Independence — separate calls to async_singleton return separate state
# ---------------------------------------------------------------------------


class TestIndependence:
    @pytest.mark.asyncio
    async def test_independent_singletons_do_not_share_state(self):
        """Two ``async_singleton`` calls must each get their own instance + lock.

        This is what lets a service hold one singleton per resource (Discogs HTTP,
        Apple Music HTTP, asyncpg pool) without them stomping on each other.
        """

        async def factory_a() -> str:
            return "A"

        async def factory_b() -> str:
            return "B"

        getter_a, _ = async_singleton(factory_a)
        getter_b, _ = async_singleton(factory_b)

        assert await getter_a() == "A"
        assert await getter_b() == "B"
        # And the cached values stay distinct on subsequent calls.
        assert await getter_a() == "A"
        assert await getter_b() == "B"
