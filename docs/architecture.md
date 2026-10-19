# Architecture

## Layout

```
src/wxyc_fastapi/
├── __init__.py            # __version__
├── testing.py             # v1.4.0 — CountingPosthog + capture_budget (consumer test helpers)
├── observability/         # v0.1.0 — Phase A
│   ├── __init__.py        # re-exports (grows as PRs land)
│   ├── cache_stats.py     # PR 1: init_cache_stats, CacheStatsRecorder, timed_pg, timed_api
│   ├── sentry.py          # PR 2: init_sentry, add_breadcrumb, capture_exception
│   ├── posthog.py         # PR 2: get_posthog_client (warn-once), flush, shutdown; v1.4.0 capture rate limiter
│   └── telemetry.py       # PR 3: RequestTelemetry, StepResult, track_step, send_to_posthog
├── healthcheck/           # v0.2.0 — Phase B
│   ├── __init__.py        # re-exports
│   ├── liveness.py        # liveness_router (GET /health)
│   └── readiness.py       # readiness_router(checks, *, timeout=...) factory + Check dataclass
├── http/                  # v0.3.0 — Phase D
│   ├── __init__.py        # re-exports
│   └── singleton.py       # async_singleton(factory) -> (getter, closer)
└── db/                    # v1.0.0 — Phase E
    ├── __init__.py        # re-exports
    └── lazy_pg.py         # LazyPgConnection (sync psycopg, lifted from semantic-index/utils.py)
tests/
├── conftest.py            # cache-stats ContextVar isolation fixture
├── observability/
│   ├── test_cache_stats.py  # PR 1
│   ├── test_sentry.py       # PR 2
│   ├── test_error_tracking.py  # v1.6.0
│   ├── test_posthog.py      # PR 2
│   ├── test_posthog_ratelimit.py  # v1.4.0
│   └── test_telemetry.py    # PR 3
├── healthcheck/
│   ├── fixtures/
│   │   └── api-yaml-schemas.json   # Phase C: vendored wxyc-shared schemas
│   ├── test_conformance.py         # Phase C: api.yaml ↔ ReadinessResponse parity
│   ├── test_liveness.py
│   └── test_readiness.py
├── http/
│   └── test_singleton.py           # Phase D: LML#241 reproducer + closer + retry semantics
├── db/
│   └── test_lazy_pg.py             # Phase E: lazy connect + reconnect + connect-failure retry
└── testing/
    └── test_capture_budget.py      # v1.4.0: CountingPosthog + capture_budget
scripts/
└── sync-api-yaml-schemas.py        # refresh the conformance fixture from a wxyc-shared tag
```

## Public API surface

PR 3 completes the v0.1.0 surface. Consumers should import from `wxyc_fastapi.observability`, not the submodules:

```python
from wxyc_fastapi.observability import (
    init_sentry, add_breadcrumb, capture_exception,
    RequestTelemetry, StepResult,
    init_cache_stats, get_cache_stats_recorder, get_cache_stats, CacheStatsRecorder,
    timed_pg, timed_api,
    get_posthog_client, flush_posthog, shutdown_posthog,
)
```

Consumer *test suites* additionally import the capture-budget helpers from `wxyc_fastapi.testing` (v1.4.0):

```python
from wxyc_fastapi.testing import CountingPosthog, capture_budget
```

## Per-consumer parameterization

The shared modules are parameterized so each consumer can keep its existing taxonomy. When migrating a consumer, the per-consumer values are:

| Parameter | LML | rom | semantic-index |
|---|---|---|---|
| `init_sentry(service_name=...)` | `"library-metadata-lookup"` | `"request-o-matic"` | `"semantic-index"` |
| `init_cache_stats(extra_keys=...)` | `None` | `["memory_misses"]` | `None` |
| `RequestTelemetry.api_call_keys` | `["discogs"]` | `["groq", "discogs", "slack"]` | `[]` |
| `RequestTelemetry.distinct_id` | `"library-metadata-lookup-service"` | `"request-o-matic-service"` | `"semantic-index-service"` |
| `RequestTelemetry.event_prefix` | `"lookup"` | `"request"` | `"explore"` |
| `get_posthog_client(event_prefix=...)` | `"lookup"` | `"request"` | `"explore"` (no-op; semantic-index doesn't install the `[posthog]` extra) |

`HttpxIntegration` is default-on in `init_sentry` — see CHANGELOG's "Consumer impact" line. Pass `integrations=[FastApiIntegration()]` to opt out.

## Versioning

| Version | Phase | Adds |
|---|---|---|
| 0.1.0 | A | observability/ (cache_stats + sentry + posthog + telemetry; ships after PR 3) |
| 0.2.0 | B | healthcheck/ |
| 0.3.0 | D | http/singleton (async-singleton + lock helper) |
| 1.0.0 | E | db/lazy_pg (sync psycopg, lifted verbatim from semantic-index `utils.py:14-42`) |
| 1.1.0 | — | `RequestTelemetry.as_server_timing()` — serialize tracked steps to a `Server-Timing` header (additive; see [`observability.md`](observability.md#telemetry)) |
| 1.2.0 | — | `as_server_timing(descriptions=...)` — per-leg `desc="..."` annotation for derived/overlapping legs, sanitized against header injection (additive; see [`observability.md`](observability.md#telemetry)) |
| 1.3.0 | — | `RequestTelemetry(emit_step_events=False)` — suppress unconsumed per-step PostHog events, summary-only (additive; see [`observability.md`](observability.md#telemetry)) |
| 1.4.0 | — | PostHog capture rate limiter (token-bucket proxy from `get_posthog_client`, `telemetry_rate_limited` drop reports) + `wxyc_fastapi.testing` capture-budget helpers (see [`observability.md`](observability.md#posthog-wrapper)) |
| 1.6.0 | — | `init_error_tracking(...)` (PostHog error tracking: FastAPI handler, exception autocapture, `before_send`) + `capture_exception` / `add_breadcrumb` shims re-routed to PostHog; `daily_ceiling` required (see [`observability.md`](observability.md#init_error_tracking)) |

The package follows SemVer. Now that 1.0 is cut, consumers pin the major range `wxyc-fastapi>=1.x,<2` (e.g. a consumer that calls a method added in 1.1.0 pins `>=1.1.0,<2.0.0`); a breaking change bumps to 2.0.0.

## Dependencies

- Required: `fastapi>=0.110`, `httpx>=0.25`, `sentry-sdk[fastapi]>=2.0`
- Extras: `[posthog]` (used by PR 2's posthog module), `[psycopg]` (Phase E `db.lazy_pg`), `[asyncpg]` (forward-looking, no consumer today), `[dev]`

`fastapi` and `sentry-sdk` are required deps even at PR 1 because the eventual v0.1.0 surface needs them; pyproject.toml is forward-looking so subsequent PRs don't need to bump the dep set.
