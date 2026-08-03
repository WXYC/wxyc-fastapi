# Changelog

All notable changes to this project will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [1.3.0] - 2026-08-03

Adds an `emit_step_events` toggle to `RequestTelemetry` so a high-volume consumer can emit only the `<prefix>_completed` summary and suppress the per-step PostHog events. Motivated by a Backend-Service backfill campaign that started 2026-08-01: LML's `/lookup` registers ~9 `track_step` legs, so the default fans one lookup out to ~10 PostHog events, and the backfill multiplied PostHog ingestion into the "Request-O-Matic" project ~7× (≈60k lookups/day → ≈440k events/day). None of the LML per-step events (`lookup_library_search`, `lookup_album_lookup`, `lookup_streaming_status`, …) are read by any insight or alert — only `lookup_completed` is — so they were pure ingestion cost. The per-step timings are already carried in the summary's `steps` property, so suppressing the per-step events loses no *timing* data; the only fields dropped are the per-step `success`/`error_type` (equally unconsumed for LML `/lookup`), which are the reason the toggle is opt-in per route rather than a global default.

### Added
- `RequestTelemetry.emit_step_events: bool = True` — new keyword-only constructor field. When `True` (default) `send_to_posthog` emits one `<prefix>_<step>` event per tracked step plus the `<prefix>_completed` summary, exactly as before. When `False`, only the summary fires; the per-step durations remain available under `summary["steps"]` (`{<step>_ms: <duration>}`), though the per-step `success`/`error_type` are not (they lived only on the suppressed events). It is keyword-only so that inserting it leaves the positional argument order unchanged — `steps` stays the 4th positional field, so positional construction is backward-compatible. The toggle is per-`RequestTelemetry` instance, so a consumer can suppress on one route while leaving others at the default.
- `TestEmitStepEvents` (`tests/observability/test_telemetry.py`) — default-`True`, `False`-suppresses-per-step-keeping-only-summary, `False`-still-carries-every-step-timing-in-summary, and an explicit-`True`-matches-historical-behavior regression guard.

### Consumer impact
- Additive and backward-compatible (minor bump). The field defaults to `True`, so every existing call site — including request-o-matic, whose "Average Duration by Step" dashboards read the `request_<step>` per-step events, and LML's `streaming_check` / `artist_resolve` routes — is byte-identical with no code change. A consumer re-pins `>=1.1.0,<2.0.0` → `>=1.3.0,<2.0.0` only when it passes `emit_step_events=False` (`1.1.0` is the pin baseline because `1.2.0` never reached PyPI — see the release note below). First consumer: library-metadata-lookup, on its three `/lookup` `RequestTelemetry` construction sites.
- **Release note:** `v1.2.0` was tagged in this CHANGELOG but never pushed, so no `1.2.0` artifact reached PyPI (latest published was `1.1.0`). `1.3.0` is the first release cut from that baseline and therefore ships both the 1.2.0 `as_server_timing(descriptions=)` work and this toggle. Consumers needing only the `descriptions=` surface may still pin `>=1.3.0,<2.0.0`.

[1.3.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v1.3.0

## [1.2.0] - 2026-07-27

Adds per-leg `desc` support to `RequestTelemetry.as_server_timing()`. Some legs are derived or overlapping rather than additive — LML's `discogs` leg (`pg_time_ms + api_time_ms`) double-counts time already reflected in another span, so summing every `dur` in the header does not reconstruct the total. A `desc="overlaps album_lookup and is not additive"` annotation makes that legible in the header itself instead of relying on out-of-band documentation.

### Added
- `RequestTelemetry.as_server_timing(extra=None, descriptions=None) -> str` — `descriptions` is a strictly opt-in `{leg_name: text}` map (any tracked-step name, `extra` key, or `"total"`); a named leg renders as `name;desc="<sanitized>";dur=<ms>` instead of the plain `name;dur=<ms>` form. Text is run through the new `_sanitize_desc` helper before embedding: it is coerced to a printable-ASCII whitelist (`0x20`-`0x7e`) — every non-ASCII code point and C0/C1/DEL control character is dropped, so the value always survives the consumer's latin-1 header encode rather than raising `UnicodeEncodeError` — then the header's structural delimiters `,` `;` `=` are stripped (a downstream parser like request-o-matic's `parse_server_timing` splits on `,`/`;` without quoted-string awareness, and dropping `=` is defense-in-depth against a parser that scans for `dur=` anywhere in an entry), while `"` and `\` are escaped per the RFC 7230 `quoted-string` grammar (`\"`, `\\`). If sanitizing empties the text, the leg falls back to the plain `dur`-only form. Existing callers passing only `extra` (or nothing) are unaffected — output is byte-identical to pre-1.2.0.
- `TestAsServerTimingDescriptions` (`tests/observability/test_telemetry.py`) — backward-compatibility with no `descriptions`, `desc` on both an `extra` leg and a tracked step, unknown-key and no-description leg behavior, a parametrized `_sanitize_desc` table covering each stripped/escaped character class (structural `,`/`;`/`=`, CR/LF, C0/C1/DEL controls, non-ASCII Latin-1/CJK/emoji, and the U+2028/U+2029 line separators), a fallback-to-plain-leg case when a `desc` sanitizes to empty, a non-ASCII case asserting the whole header stays latin-1-encodable, and a centerpiece test asserting that a `desc` containing `"`, `;`, `,`, and a CR/LF sanitizes cleanly and the resulting header still parses to exactly one `total` with all `dur` values preserved.

### Consumer impact
- Additive and backward-compatible (minor bump). Existing call sites are unchanged; a consumer re-pins `>=1.1.0,<2.0.0` → `>=1.2.0,<2.0.0` only when it calls `as_server_timing` with `descriptions=`. First consumer: library-metadata-lookup, annotating its derived `discogs` leg.

[1.2.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v1.2.0

## [1.1.0] - 2026-07-16

Adds `RequestTelemetry.as_server_timing()` so a service can surface its already-captured per-stage step timings as a [`Server-Timing`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Server-Timing) response header. Motivated by a `lookup` CLI round-trip that measured 8.99 s while only the Discogs `cache_stats` (404 ms) was visible to the caller — the ~8.5 s spent in library-metadata-lookup's `metadata_enrichment` stage was captured by `track_step` but never reached the client. The header is the missing read surface; no new capture layer.

### Added
- `RequestTelemetry.as_server_timing(extra=None) -> str` — serializes each tracked step (`<name>;dur=<ms>`, insertion order), then one entry per `extra` item, then exactly one live `total` from `get_total_duration_ms()`, `", "`-joined per the `Server-Timing` grammar. `extra` carries durations measured outside `track_step` (library-metadata-lookup's derived `discogs` = `pg_time_ms + api_time_ms`; request-o-matic's forwarded LML sub-stages) and must not contain a `total` key. Durations render at two-decimal precision with trailing zeros stripped (`8102.00` → `8102`) and always as fixed-point so multi-second values stay parseable as plain floats.
- `TestAsServerTiming` (7 tests) — grammar/ordering, `extra` merge position, single-`total` invariant, float formatting, empty-steps → total-only, per-`dur` float round-trip.

### Consumer impact
- Additive and backward-compatible (minor bump). Existing call sites are unchanged; a consumer re-pins `>=1.0.0,<2.0.0` → `>=1.1.0,<2.0.0` only when it calls the new method. First consumers: library-metadata-lookup (emit on `/lookup`) and request-o-matic (forward + merge on `/request`, print in the `lookup` CLI).

[1.1.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v1.1.0

## [1.0.0] - 2026-05-12

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
