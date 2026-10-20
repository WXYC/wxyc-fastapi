# Claude Code Instructions for wxyc-fastapi

## Project overview

`wxyc-fastapi` is the shared FastAPI service template extracted from `library-metadata-lookup`, `request-o-matic`, and `semantic-index`. It carries the primitives every WXYC FastAPI service needs (PostHog error tracking, request telemetry, cache-stats recorder, PostHog wrapper) and lets the next fix land once instead of three times.

The motivation, drift inventory, phasing (A → E), and decisions are in [the wiki plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md). Project board: [WXYC #29](https://github.com/orgs/WXYC/projects/29). Epic: [#1](https://github.com/WXYC/wxyc-fastapi/issues/1).

Phases A–E all shipped (Phase A was issue [#2](https://github.com/WXYC/wxyc-fastapi/issues/2), three sequential PRs: scaffolding + `cache_stats`; `sentry` + `posthog`; `telemetry`). The package is on PyPI at 1.x; see `docs/architecture.md` for the versioning ladder and `CHANGELOG.md` for what each release added.

## Topic guides

CLAUDE.md is a router for the always-loaded reference card. Topic depth lives in `docs/`:

- **[`docs/architecture.md`](docs/architecture.md)** — Package layout, public API surface (`wxyc_fastapi.observability` import sites), per-consumer parameterization table (LML / rom / semantic-index), versioning ladder (0.1 → 1.0 across phases A/B/D/E), dependency policy
- **[`docs/observability.md`](docs/observability.md)** — Cache stats ContextVar contract + `extra_keys` shape-guarantee semantics, PostHog wrapper (lazy import, warn-once on missing key), `RequestTelemetry` step + API-call tracking, `event_prefix` namespacing
- **[`docs/healthcheck.md`](docs/healthcheck.md)** — `liveness_router` / `readiness_router(checks, *, timeout=...)` factory, `Check` semantics + aggregation rules, `wxyc-shared/api.yaml` conformance test (Phase C), fixture refresh recipe
- **[`docs/http-singleton.md`](docs/http-singleton.md)** — `async_singleton(factory)` double-check-lock rationale (LML#241 FD-leak race), closer dispatch across `aclose()` / coroutine-`close()` / sync-`close()`, factory-exception retry semantics
- **[`docs/db-lazy-pg.md`](docs/db-lazy-pg.md)** — `LazyPgConnection` sync-psycopg wrapper (lifted from semantic-index), graceful-degradation `None` return contract, why this is sync-only and async services should use `async_singleton(asyncpg.create_pool)` instead
- **[`docs/release.md`](docs/release.md)** — Tag-pushed PyPI Trusted Publishing flow, CHANGELOG "Consumer impact" rule
- **[`docs/migration-playbook.md`](docs/migration-playbook.md)** — Step-by-step recipe for consumer-side tickets A2/A3/A4 (LML, rom, semantic-index): pyproject pin, swap `core/telemetry.py`, cache-stats free-function → recorder-method migration

Read the relevant topic doc before doing work in that area.

## Development

### Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,posthog,psycopg]'
```

### Test + lint

```bash
.venv/bin/pytest                       # full suite: observability + healthcheck + http + db + testing helpers (incl. Phase C conformance)
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

CI runs both jobs on Linux 3.12 only at v0 (per [§5 decision #6](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md#5-open-decisions)). Add 3.13 + macOS once we have a real consumer pinning either.

### TDD discipline

Per the WXYC global convention, every change in this repo follows red → green → refactor:

1. Write a failing test that pins the desired behavior.
2. Implement the minimum to pass.
3. Refactor.

The cache-stats ContextVar isolation is a recurring footgun — pytest reuses the same execution context across tests, so a synchronous `_cache_stats_var.set(...)` in test N persists into test N+1's `asyncio.run()` block. The autouse fixture in `tests/conftest.py` replaces the module-level `_cache_stats_var` with a fresh `ContextVar` per test. New tests get this isolation for free; do not call `_cache_stats_var.set(...)` directly from production code outside of `init_cache_stats()`.

## Related repos

- [`library-metadata-lookup`](https://github.com/WXYC/library-metadata-lookup), [`request-o-matic`](https://github.com/WXYC/request-o-matic), [`semantic-index`](https://github.com/WXYC/semantic-index) — the three migration targets for v0.1.0.
- [`wxyc-shared`](https://github.com/WXYC/wxyc-shared) — Phase C `HealthCheckResponse` / `ReadinessResponse` schemas land here, not in this package.
- [`Backend-Service`](https://github.com/WXYC/Backend-Service) — TS sibling; the `HealthCheckResponse` contract is consumed there too via OpenAPI codegen (Phase C tracking issue [#804](https://github.com/WXYC/Backend-Service/issues/804)).
