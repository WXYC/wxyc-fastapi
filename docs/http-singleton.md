# Http singleton

`wxyc_fastapi.http.async_singleton(factory)` returns a `(getter, closer)` pair backing a lazy async singleton. The getter implements double-check-lock so concurrent first-callers see exactly one `factory()` invocation — without that lock, every caller passes the outer `is None` check, each constructs a fresh client/pool, and only one survives as the cached value while the others are orphaned with their connections (and FDs) still open. This is the FD-leak race documented in [LML#241](https://github.com/WXYC/library-metadata-lookup/issues/241) and fixed in [LML#242](https://github.com/WXYC/library-metadata-lookup/pull/242); extracting the pattern here means the next async singleton can't be written without the lock.

The closer dispatches teardown across the three shapes seen in WXYC's stack: `aclose()` (httpx `AsyncClient`), `close()` returning a coroutine (`asyncpg.Pool`), and `close()` returning `None` (sync, e.g. `posthog.Posthog`). It captures-then-clears the cached instance before invoking teardown so a concurrent `await getter()` rebuilds via the factory rather than racing against a half-torn-down resource.

Factory exception leaves the singleton unset and releases the lock (via `async with`), so the next `await getter()` retries — a single transient init failure must not permanently wedge the service into a half-built state.

The headline regression test is `tests/http/test_singleton.py::TestConcurrentFirstCallers::test_factory_invoked_once_under_concurrency`: 50 concurrent first-callers must see one factory invocation. Removing `async with lock:` from `singleton.py` fails it (50 invocations instead of 1).
