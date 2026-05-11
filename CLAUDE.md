# Claude Code Instructions for wxyc-fastapi

## Project overview

`wxyc-fastapi` is the shared FastAPI service template extracted from `library-metadata-lookup`, `request-o-matic`, and `semantic-index`. It carries the primitives every WXYC FastAPI service needs (Sentry init, request telemetry, cache-stats recorder, PostHog wrapper) and lets the next fix land once instead of three times.

The motivation, drift inventory, phasing (A → E), and decisions are in [the wiki plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md). Project board: [WXYC #29](https://github.com/orgs/WXYC/projects/29). Epic: [#1](https://github.com/WXYC/wxyc-fastapi/issues/1).

Phase A (issue [#2](https://github.com/WXYC/wxyc-fastapi/issues/2)) is split across three sequential PRs:

1. **PR 1**: package scaffolding + `cache_stats` module
2. **PR 2 (this branch)**: `sentry` + `posthog` modules (depends on PR 1)
3. **PR 3**: `telemetry` module (depends on PR 1's `cache_stats`)

All three roll up into the v0.1.0 PyPI release, tagged after PR 3 merges.

## Layout

```
src/wxyc_fastapi/
├── __init__.py            # __version__
├── observability/         # v0.1.0 — Phase A
│   ├── __init__.py        # re-exports (grows as PRs land)
│   ├── cache_stats.py     # PR 1: init_cache_stats, CacheStatsRecorder, timed_pg, timed_api
│   ├── sentry.py          # PR 2: init_sentry, add_breadcrumb, capture_exception
│   ├── posthog.py         # PR 2: get_posthog_client (warn-once), flush, shutdown
│   └── telemetry.py       # PR 3: RequestTelemetry, StepResult, track_step, send_to_posthog
├── healthcheck/           # v0.2.0 — Phase B (pending)
├── http/                  # v0.3.0 — Phase D (pending)
└── db/                    # v1.0.0 — Phase E (pending)
tests/
├── conftest.py            # cache-stats ContextVar isolation fixture
└── observability/
    ├── test_cache_stats.py  # PR 1
    ├── test_sentry.py       # PR 2
    ├── test_posthog.py      # PR 2
    └── test_telemetry.py    # PR 3
```

## Public API surface (this branch)

PR 2 ships `cache_stats` (from PR 1) plus the new `sentry` and `posthog` modules. Consumers should import from `wxyc_fastapi.observability`, not the submodules:

```python
from wxyc_fastapi.observability import (
    # cache_stats (PR 1)
    init_cache_stats, get_cache_stats_recorder, get_cache_stats,
    CacheStatsRecorder, timed_pg, timed_api,
    # sentry (PR 2)
    init_sentry, add_breadcrumb, capture_exception,
    # posthog (PR 2)
    get_posthog_client, flush_posthog, shutdown_posthog,
)
```

## Per-consumer parameterization

When migrating a consumer, the per-consumer values are:

| Parameter | LML | rom | semantic-index |
|---|---|---|---|
| `init_sentry(service_name=...)` | `"library-metadata-lookup"` | `"request-o-matic"` | `"semantic-index"` |
| `init_cache_stats(extra_keys=...)` | `None` | `["memory_misses"]` | `None` |
| `get_posthog_client(event_prefix=...)` | `"lookup"` | `"request"` | `"explore"` (no-op; semantic-index doesn't install the `[posthog]` extra) |

`HttpxIntegration` is default-on in `init_sentry` — see CHANGELOG's "Consumer impact" line. Pass `integrations=[FastApiIntegration()]` to opt out.

## Cache stats: behavior recap

`init_cache_stats` seeds the per-request stats dict with six base keys (`memory_hits`, `pg_hits`, `pg_misses`, `api_calls`, `pg_time_ms`, `api_time_ms`); `extra_keys=[...]` adds consumer-specific metrics. `CacheStatsRecorder` exposes well-named helpers (`record_memory_cache_hit`, `record_pg_cache_miss`, `record_pg_time(ms)`, ...) plus a generic `record(key, value)` for keys passed via `extra_keys`. All recorder methods are no-ops when `init_cache_stats()` has not been called for the current async context, so library code can call them unconditionally.

## PostHog wrapper

`get_posthog_client(event_prefix)` returns a lazy singleton. The `event_prefix` argument is purely diagnostic — it surfaces in the warn-once log line that fires when `POSTHOG_API_KEY` is missing, so co-located services each get one warning instead of one per call. The returned client is shared across callers; events are **not** auto-prefixed.

The `posthog` SDK is imported inside `get_posthog_client` on first successful call (when an API key is set). Consumers that never call it do not need the `[posthog]` extra installed — semantic-index relies on this.

## Versioning

| Version | Phase | Adds |
|---|---|---|
| 0.1.0 | A | observability/ (cache_stats + sentry + posthog + telemetry; ships after PR 3) |
| 0.2.0 | B | healthcheck/ |
| 0.3.0 | D | http/singleton + db/lazy-pool init |
| 1.0.0 | E | db/lazy_pg with double-check-lock pattern |

The package follows SemVer; consumers pin a minor range (`wxyc-fastapi>=0.1,<0.2`) until 1.0.

## Dependencies

- Required: `fastapi>=0.110`, `httpx>=0.25`, `sentry-sdk[fastapi]>=2.0`
- Extras: `[posthog]` (used by PR 2's posthog module), `[asyncpg]` (Phase E), `[dev]`

`fastapi` and `sentry-sdk` are required deps even at PR 1 because the eventual v0.1.0 surface needs them; pyproject.toml is forward-looking so subsequent PRs don't need to bump the dep set.

## Development

### Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev,posthog]'
```

### Test + lint

```bash
.venv/bin/pytest                       # 44 tests at PR 2 (cache_stats + sentry + posthog); grows to 58 at PR 3
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

## Release

Tag-pushed: `git tag v0.1.0 && git push origin v0.1.0` triggers `.github/workflows/publish.yml` which builds + uploads via PyPI Trusted Publishing (no token in repo). The tag is cut after PR 3 merges, not PR 1 or PR 2.

The PyPI Trusted Publisher relationship must be configured once in the PyPI UI before the first tag push (project name `wxyc-fastapi`, owner `WXYC`, repo `wxyc-fastapi`, workflow `publish.yml`, environment `pypi`).

CHANGELOG.md must be updated for every PR. The "Consumer impact" subsection is non-optional when the change affects a consumer's observable behavior (e.g., the `HttpxIntegration` default-on flip in PR 2's sentry module).

## Related repos

- [`library-metadata-lookup`](https://github.com/WXYC/library-metadata-lookup), [`request-o-matic`](https://github.com/WXYC/request-o-matic), [`semantic-index`](https://github.com/WXYC/semantic-index) — the three migration targets for v0.1.0.
- [`wxyc-shared`](https://github.com/WXYC/wxyc-shared) — Phase C `HealthCheckResponse` / `ReadinessResponse` schemas land here, not in this package.
- [`Backend-Service`](https://github.com/WXYC/Backend-Service) — TS sibling; the `HealthCheckResponse` contract is consumed there too via OpenAPI codegen (Phase C tracking issue [#804](https://github.com/WXYC/Backend-Service/issues/804)).
