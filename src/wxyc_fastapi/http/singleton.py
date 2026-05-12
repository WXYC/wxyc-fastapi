"""Double-check-lock async singleton for lazy resources (HTTP clients, pools).

Why this exists
---------------
[LML#241](https://github.com/WXYC/library-metadata-lookup/issues/241) was an
FD-leak race in lazy ``httpx.AsyncClient`` and ``asyncpg.Pool`` initialization:
concurrent first-callers each passed an outer ``if instance is None`` check,
each constructed a fresh client/pool, and only one survived as the module-level
singleton — the rest were orphaned with their connections (and file
descriptors) still open. The
[LML#242](https://github.com/WXYC/library-metadata-lookup/pull/242) fix added
an ``asyncio.Lock`` and an inner re-check; this module extracts that pattern so
the next async singleton can't be written without the lock.

Usage
-----
::

    import httpx
    from wxyc_fastapi.http import async_singleton

    async def _build_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0)

    get_apple_music_client, close_apple_music_client = async_singleton(_build_client)

    # In a request handler:
    client = await get_apple_music_client()

    # In an app shutdown hook:
    await close_apple_music_client()

The closer dispatches between three teardown shapes seen in WXYC's stack:

* ``aclose()`` — async cleanup (``httpx.AsyncClient``).
* ``close()`` returning a coroutine — async cleanup with the older naming
  (``asyncpg.Pool``); the closer awaits the returned coroutine.
* ``close()`` returning ``None`` — sync cleanup (``posthog.Posthog``).

If the factory raises, the singleton stays unset (the lock is released by
``async with`` on exception); the next ``await getter()`` will retry. This
prevents a single transient init failure from permanently wedging the service
into a half-built state.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable


def async_singleton[T](
    factory: Callable[[], Awaitable[T]],
) -> tuple[Callable[[], Awaitable[T]], Callable[[], Awaitable[None]]]:
    """Build a (getter, closer) pair backing a lazy async singleton.

    The getter implements double-check-lock so concurrent first-callers see
    exactly one ``factory()`` invocation. The closer tears down the singleton
    and resets state so the next ``getter()`` will rebuild via the factory.

    Args:
        factory: Async callable that constructs the underlying resource.
            Called at most once per (getter, closer) pair *per init cycle* —
            once at first ``await getter()``, and again only after a
            ``await closer()`` resets the state.

    Returns:
        A ``(getter, closer)`` tuple. ``getter()`` returns the singleton
        instance. ``closer()`` cleans up the instance (``aclose`` if present;
        otherwise ``close``, awaited if it returns a coroutine) and clears the
        cached value. Both are safe to call concurrently with themselves and
        with each other; ``closer()`` before any ``getter()`` is a no-op.
    """
    instance: T | None = None
    lock = asyncio.Lock()

    async def getter() -> T:
        nonlocal instance
        if instance is not None:
            return instance
        async with lock:
            if instance is None:
                instance = await factory()
            return instance

    async def closer() -> None:
        nonlocal instance
        if instance is None:
            return
        # Capture-then-clear so a concurrent getter sees ``None`` immediately
        # (and rebuilds via the factory) rather than racing against a
        # half-torn-down resource.
        to_close = instance
        instance = None
        if hasattr(to_close, "aclose"):
            await to_close.aclose()
        elif hasattr(to_close, "close"):
            result = to_close.close()
            if inspect.isawaitable(result):
                await result

    return getter, closer
