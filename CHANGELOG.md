# Changelog

All notable changes to this project will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] - Unreleased

Phase E of the [wxyc-fastapi plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md): the `db.lazy_pg` module — a lazy, reconnecting sync `psycopg` connection wrapper lifted verbatim from semantic-index's `utils.py`. The 1.0.0 cut signals API stability — the package surface (`observability/*`, `healthcheck/*`, `http/singleton`, `db/lazy_pg`) is now considered settled. Closes [WXYC/wxyc-fastapi#6](https://github.com/WXYC/wxyc-fastapi/issues/6) and the [#1](https://github.com/WXYC/wxyc-fastapi/issues/1) epic on consumer migration.

### Added
- `wxyc_fastapi.db.lazy_pg.LazyPgConnection(dsn, label)` — defers `psycopg.connect` until first `get()`, transparently reconnects when the underlying connection is closed (server-side disconnect, idle timeout, pool eviction), returns `None` when `dsn is None` or when `psycopg.connect` raises. Logs at WARNING with `exc_info` on connect failure so operators see the underlying `OperationalError` without the pipeline aborting.
- `tests/db/test_lazy_pg.py` — 8-test suite covering: no-op when DSN is `None`; lazy first-connect with `autocommit=True` kwarg pass-through; identity caching across subsequent `get()` calls; reconnect after the cached connection's `closed` flag flips; graceful-degradation `None` return on `psycopg.connect` failure with retry on the next call; multi-instance independence.
- `[psycopg]` optional extra (`psycopg>=3.1`). Only consumers wiring `LazyPgConnection` against a real DSN need it installed; semantic-index does. The pre-existing `[asyncpg]` extra is left in place for forward-looking async pool consumers (none today).
- `tests/healthcheck/test_conformance.py` (carried in from the post-0.3.0 follow-up) — Phase C conformance test ([WXYC/wxyc-fastapi#4](https://github.com/WXYC/wxyc-fastapi/issues/4)) asserting that the local Pydantic `ReadinessResponse` model in `wxyc_fastapi.healthcheck.readiness` and the `HealthCheckResponse` / `ReadinessResponse` schemas in [`wxyc-shared/api.yaml@v0.13.0`](https://github.com/WXYC/wxyc-shared/blob/v0.13.0/api.yaml) agree on the `status` enum (`healthy`/`degraded`/`unhealthy`), the per-service value enum (`ok`/`unavailable`/`timeout`), the required-fields surface, and the `additionalProperties: true` openness. Three round-trip cases (healthy/degraded/unhealthy) validate the same payload through both schemas via `jsonschema` and the Pydantic model so the test fails if either side accepts what the other rejects. A breaking change to either side fails CI here.
- `tests/healthcheck/fixtures/api-yaml-schemas.json` — vendored snapshot of the two component schemas at the pinned wxyc-shared tag. Refresh with `python scripts/sync-api-yaml-schemas.py --ref vX.Y.Z`.
- `scripts/sync-api-yaml-schemas.py` — CLI to refresh the fixture from a configurable wxyc-shared git ref.
- `[dev]` extra picks up `jsonschema>=4.21`, `pyyaml>=6.0`, and `psycopg>=3.1` so the full local test suite runs without manual extras.

### Consumer impact
- **No change for v0.x consumers** — re-pin from `>=0.3.0,<0.4.0` to `>=1.0.0,<2.0.0`. The 1.0.0 cut is a SemVer-stability signal, not a breaking change; the existing `observability/*`, `healthcheck/*`, and `http/singleton` surfaces are unchanged.
- The `db.lazy_pg` module is sync (`psycopg`), not async (`asyncpg`). The wiki plan's [§2.1](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md) line "`LazyPgConnection` from semantic-index `utils.py:14-42`. Already mature; only consumer migration to do." pinned the lift-verbatim contract — the original is sync. Async services that need a pool should use `wxyc_fastapi.http.singleton.async_singleton` to wrap `asyncpg.create_pool` instead; that path has the double-check-lock the [LML#241](https://github.com/WXYC/library-metadata-lookup/issues/241) FD-leak race requires. The `[asyncpg]` extra remains declared for that future case.
- semantic-index migration (the only consumer today): swap `from semantic_index.utils import LazyPgConnection` for `from wxyc_fastapi.db import LazyPgConnection` in `discogs_client`, `musicbrainz_client`, `wikidata_client`, `acousticbrainz_client`, and `graph_metrics`. No behavior change.

[1.0.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v1.0.0

## [0.3.0] - Unreleased

Phase D part 1 of the [wxyc-fastapi plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md): the `http.singleton` module — a generic double-check-lock async-singleton helper that prevents the file-descriptor leak race documented in [LML#241](https://github.com/WXYC/library-metadata-lookup/issues/241) (fix in [LML#242](https://github.com/WXYC/library-metadata-lookup/pull/242)). Three consumer migrations ride along ([LML#283](https://github.com/WXYC/library-metadata-lookup/issues/283), [rom#116](https://github.com/WXYC/request-o-matic/issues/116), [LML#284](https://github.com/WXYC/library-metadata-lookup/issues/284)) so the next async-singleton can't be written without the lock.

### Added
- `wxyc_fastapi.http.singleton.async_singleton(factory)` — returns a `(getter, closer)` pair backing a lazy async singleton. The getter implements double-check-lock so concurrent first-callers see exactly one `factory()` invocation; the closer dispatches teardown across the three shapes seen in WXYC's stack: `aclose()` (httpx `AsyncClient`), `close()` returning a coroutine (`asyncpg.Pool`), and `close()` returning `None` (sync, e.g. `posthog.Posthog`).
- `tests/http/test_singleton.py` — 11-test suite. The headline `test_factory_invoked_once_under_concurrency` is the LML#241 reproducer: 50 concurrent first-callers must see one factory call. Removing the lock from `async_singleton` fails it.

### Consumer impact
- `Check.required` parallel: the factory is run *only* under the lock after the inner re-check, so it's safe to do expensive work (open pools, perform handshakes) without worrying about duplicate invocation.
- Factory exception leaves the singleton unset and releases the lock; the next `await getter()` will retry. This is intentional — a single transient init failure must not permanently wedge the service into a half-built state.
- The closer captures-then-clears the cached instance before invoking teardown so a concurrent getter sees `None` immediately and rebuilds via the factory rather than racing against a half-torn-down resource.

[0.3.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v0.3.0

## [0.2.0] - Unreleased

Phase B of the [wxyc-fastapi plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md): the `healthcheck` package — a single `liveness_router` plus a parameterized `readiness_router(checks, *, timeout=...)` that runs probes concurrently with per-probe timeouts and aggregates `healthy`/`degraded`/`unhealthy` (HTTP 503 on the latter). Replaces three divergent local healthcheck implementations in `library-metadata-lookup`, `request-o-matic`, and `semantic-index`.

### Added
- `wxyc_fastapi.healthcheck.liveness` — `liveness_router` mounted at `GET /health`. Returns `{"status": "healthy"}` without running any probes; intended for orchestrator liveness checks (Railway, Docker `HEALTHCHECK`).
- `wxyc_fastapi.healthcheck.readiness` — `readiness_router(checks, *, timeout=3.0)` factory mounted at `GET /health/ready`. Each `Check(name, probe, required=True)` is run concurrently via `asyncio.gather` with a per-probe `asyncio.wait_for(timeout)`. Probes return `"ok"` on success; raising or returning any other string is treated as `"unavailable"`; timeouts are reported as `"timeout"`. Aggregation: any required failure → `unhealthy` (HTTP 503); any optional failure → `degraded` (HTTP 200); otherwise `healthy`.
- The response shape conforms to the `HealthCheckResponse` / `ReadinessResponse` contract added to `wxyc-shared/api.yaml` in Phase C; a conformance test is filed separately ([WXYC/wxyc-fastapi#4](https://github.com/WXYC/wxyc-fastapi/issues/4)).

### Consumer impact
- `Check.required` defaults to `True` — the safe default, since forgetting to mark a probe required would silently downgrade an unhealthy service to `degraded`. Migrating consumers must explicitly set `required=False` for non-critical dependencies (e.g. an upstream metadata service whose unavailability does not block answering requests).
- The 3.0-second default per-probe timeout matches LML's prior behaviour. rom and semantic-index were unbounded; their migration tickets should review their probes for ones that may legitimately exceed 3s and either tune `timeout=` or shorten the probe.

[0.2.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v0.2.0

## [0.1.0] - Unreleased

Phase A of the [wxyc-fastapi plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md): the observability modules extracted from the FastAPI sibling services (`library-metadata-lookup`, `request-o-matic`, `semantic-index`). Phase A shipped across three sequential PRs that all roll up into v0.1.0.

### Added — PR 1: scaffolding + cache_stats
- Package layout: `src/` with `wxyc_fastapi.observability` namespace, `py.typed` marker, ruff lint+format config, pytest+pytest-asyncio, GitHub Actions CI (lint + tests on Linux 3.12), GitHub Actions publish workflow (PyPI Trusted Publishing on `v*.*.*` tag push).
- Optional extras: `[posthog]` (used by PR 2's posthog module), `[asyncpg]` (reserved for Phase E `lazy_pg` helper), `[dev]`.
- `wxyc_fastapi.observability.cache_stats` — `init_cache_stats(extra_keys=...)`, `CacheStatsRecorder` (returned by `get_cache_stats_recorder()`), `get_cache_stats`, `timed_pg`, `timed_api`. Backed by a `ContextVar` so per-request stats stay isolated across concurrent requests.

### Added — PR 2: sentry + posthog
- `wxyc_fastapi.observability.sentry` — `init_sentry`, `add_breadcrumb`, `capture_exception`. `init_sentry` sets a `service.name` tag, accepts a `before_send` filter, and defaults `environment="local"` so a stray init does not pollute the production stream.
- `wxyc_fastapi.observability.posthog` — `get_posthog_client(event_prefix)` lazy singleton with warn-once-per-prefix on missing `POSTHOG_API_KEY`; `flush_posthog`, `shutdown_posthog`. The `posthog` SDK import is lazy (inside `get_posthog_client`'s body) so consumers like `semantic-index` that don't use PostHog can `import wxyc_fastapi.observability` without installing the `[posthog]` extra.

### Added — PR 3: telemetry
- `wxyc_fastapi.observability.telemetry` — `RequestTelemetry(api_call_keys, distinct_id, event_prefix)`, `StepResult`, `track_step` context manager. Parameterizes the per-consumer API-call dict (rom: `["groq","discogs","slack"]`; LML: `["discogs"]`) and the PostHog event prefix (`lookup_*`, `request_*`). `send_to_posthog` reads cache stats from the `cache_stats` module and emits per-step events plus a `<event_prefix>_completed` summary.

### Consumer impact
- `HttpxIntegration` is **default-on** in `init_sentry`. This is the desired state per [the plan §5 decision #3](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md#5-open-decisions) — `request-o-matic` already had it; LML did not. Migrating LML onto this package will surface previously-untraced outbound httpx calls (Discogs, Spotify, Deezer, Apple Music, Bandcamp) in Sentry. The LML migration ticket ([WXYC/library-metadata-lookup#281](https://github.com/WXYC/library-metadata-lookup/issues/281)) should pre-validate against the Sentry quota before flipping.
- Consumers that want to opt out can pass `integrations=[FastApiIntegration()]` explicitly to `init_sentry`.

[0.1.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v0.1.0
