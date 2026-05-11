# wxyc-fastapi

Shared FastAPI service template for WXYC's Python services. Consolidates the duplicated service scaffolding currently forked across [library-metadata-lookup (LML)](https://github.com/WXYC/library-metadata-lookup), [request-o-matic (rom)](https://github.com/WXYC/request-o-matic), and [semantic-index](https://github.com/WXYC/semantic-index) into a single package, eliminates measurable drift between the three forks, and gives the next FastAPI service a template to inherit instead of fork.

**Status:** scaffolding. The implementation plan is at [`WXYC/wiki/plans/wxyc-fastapi`](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md). The rollout is tracked in the org-level project board *wxyc-fastapi rollout* and traces back to **P1** of the [2026-05-10 cross-repo contract audit (WXYC/wiki#51)](https://github.com/WXYC/wiki/issues/51).

## Package surface (planned)

```
wxyc_fastapi/
├── observability/
│   ├── sentry.py        # init_sentry(), add_breadcrumb(), capture_exception()
│   ├── telemetry.py     # RequestTelemetry, StepResult, track_step
│   ├── cache_stats.py   # ContextVar, init_cache_stats, record_*, get_cache_stats
│   └── posthog.py       # get_posthog_client(event_prefix), flush_posthog(), shutdown_posthog()
├── http/
│   └── singleton.py     # async_singleton(factory) - getter+closer pair w/ asyncio.Lock
├── healthcheck/
│   ├── liveness.py      # 200 OK with {"status":"healthy"} body, no probes
│   └── readiness.py     # readiness_router(checks: list[Check]) -> APIRouter
└── db/
    └── lazy_pg.py       # LazyPgConnection
```

Cross-language consolidation (codegen and healthcheck response shape) lives in [`wxyc-shared`](https://github.com/WXYC/wxyc-shared) instead. See the plan's §2.4 for what lives where and why.

## Phasing

| Phase | Module | Version |
|---|---|---|
| A | `observability/*` (sentry, telemetry, cache_stats, posthog) | v0.1.0 |
| B | `healthcheck/*` (Python implementation) | v0.2.0 |
| C | Healthcheck response schemas in `wxyc-shared/api.yaml` (parallel with B) | (no `wxyc-fastapi` version bump) |
| D | `http/singleton` | v0.3.0 |
| E | `db/lazy_pg` | v1.0.0 |

## Contributing

Implementation and migration tasks are filed as sub-issues of the rollout epic in this repo. See the [issues tab](https://github.com/WXYC/wxyc-fastapi/issues) and the project board for what's open.
