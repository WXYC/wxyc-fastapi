# Changelog

All notable changes to this project will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

Phase A of the [wxyc-fastapi plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md): the observability modules extracted from the FastAPI sibling services (`library-metadata-lookup`, `request-o-matic`, `semantic-index`). Phase A ships across three PRs that all roll up into the v0.1.0 release; each PR appends to this entry.

### Added — PR 1: scaffolding + cache_stats
- Package layout: `src/` with `wxyc_fastapi.observability` namespace, `py.typed` marker, ruff lint+format config, pytest+pytest-asyncio, GitHub Actions CI (lint + tests on Linux 3.12), GitHub Actions publish workflow (PyPI Trusted Publishing on `v*.*.*` tag push).
- Optional extras: `[posthog]` (reserved for the Phase A PostHog wrapper, lands in PR 2), `[asyncpg]` (reserved for Phase E `lazy_pg` helper), `[dev]`.
- `wxyc_fastapi.observability.cache_stats` — `init_cache_stats(extra_keys=...)`, `CacheStatsRecorder` (returned by `get_cache_stats_recorder()`), `get_cache_stats`, `timed_pg`, `timed_api`. Backed by a `ContextVar` so per-request stats stay isolated across concurrent requests.

### Pending — PR 2 + PR 3
- PR 2 adds `sentry` (`init_sentry`, `add_breadcrumb`, `capture_exception`) and `posthog` (`get_posthog_client`, `flush_posthog`, `shutdown_posthog`).
- PR 3 adds `telemetry` (`RequestTelemetry`, `StepResult`, `track_step`, `send_to_posthog`).

[0.1.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v0.1.0
