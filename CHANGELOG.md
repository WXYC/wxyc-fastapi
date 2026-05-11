# Changelog

All notable changes to this project will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

Phase A of the [wxyc-fastapi plan](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md): the observability modules extracted from the FastAPI sibling services (`library-metadata-lookup`, `request-o-matic`, `semantic-index`). Phase A ships across three PRs that all roll up into the v0.1.0 release; each PR appends to this entry.

### Added — PR 1: scaffolding + cache_stats
- Package layout: `src/` with `wxyc_fastapi.observability` namespace, `py.typed` marker, ruff lint+format config, pytest+pytest-asyncio, GitHub Actions CI (lint + tests on Linux 3.12), GitHub Actions publish workflow (PyPI Trusted Publishing on `v*.*.*` tag push).
- Optional extras: `[posthog]` (used by PR 2's posthog module), `[asyncpg]` (reserved for Phase E `lazy_pg` helper), `[dev]`.
- `wxyc_fastapi.observability.cache_stats` — `init_cache_stats(extra_keys=...)`, `CacheStatsRecorder` (returned by `get_cache_stats_recorder()`), `get_cache_stats`, `timed_pg`, `timed_api`. Backed by a `ContextVar` so per-request stats stay isolated across concurrent requests.

### Added — PR 2: sentry + posthog
- `wxyc_fastapi.observability.sentry` — `init_sentry`, `add_breadcrumb`, `capture_exception`. `init_sentry` sets a `service.name` tag, accepts a `before_send` filter, and defaults `environment="local"` so a stray init does not pollute the production stream.
- `wxyc_fastapi.observability.posthog` — `get_posthog_client(event_prefix)` lazy singleton with warn-once-per-prefix on missing `POSTHOG_API_KEY`; `flush_posthog`, `shutdown_posthog`. The `posthog` SDK import is lazy (inside `get_posthog_client`'s body) so consumers like `semantic-index` that don't use PostHog can `import wxyc_fastapi.observability` without installing the `[posthog]` extra.

### Consumer impact (PR 2)
- `HttpxIntegration` is **default-on** in `init_sentry`. This is the desired state per [the plan §5 decision #3](https://github.com/WXYC/wiki/blob/main/plans/wxyc-fastapi.md#5-open-decisions) — `request-o-matic` already had it; LML did not. Migrating LML onto this package will surface previously-untraced outbound httpx calls (Discogs, Spotify, Deezer, Apple Music, Bandcamp) in Sentry. The LML migration ticket ([WXYC/library-metadata-lookup#281](https://github.com/WXYC/library-metadata-lookup/issues/281)) should pre-validate against the Sentry quota before flipping.
- Consumers that want to opt out can pass `integrations=[FastApiIntegration()]` explicitly to `init_sentry`.

### Pending — PR 3
- PR 3 adds `telemetry` (`RequestTelemetry`, `StepResult`, `track_step`, `send_to_posthog`) and tags v0.1.0.

[0.1.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v0.1.0
