# Observability

The `wxyc_fastapi.observability` module ships three coupled concerns: cache stats (per-request metric dict in a ContextVar), the PostHog client wrapper, and request telemetry that drives the per-step PostHog events. Sentry init lives in the same module but is documented inline in the public-API section of [`architecture.md`](architecture.md#public-api-surface).

## Cache stats: behavior recap

`init_cache_stats` seeds the per-request stats dict with six base keys (`memory_hits`, `pg_hits`, `pg_misses`, `api_calls`, `pg_time_ms`, `api_time_ms`); `extra_keys=[...]` adds consumer-specific metrics. `extra_keys` is a *shape guarantee* — it makes sure the named keys appear in `get_cache_stats()` with value `0` even when never recorded, so PostHog event shapes stay stable across requests. It is not a permission system: `recorder.record(key, value)` and the named helpers (e.g. `record_memory_cache_miss`) will create undeclared keys on first call.

`CacheStatsRecorder` exposes well-named helpers (`record_memory_cache_hit`, `record_pg_cache_miss`, `record_pg_time(ms)`, ...) plus a generic `record(key, value)` for ad-hoc metrics. All recorder methods are no-ops when `init_cache_stats()` has not been called for the current async context, so library code can call them unconditionally.

## PostHog wrapper

`get_posthog_client(event_prefix)` returns a lazy singleton. The `event_prefix` argument is purely diagnostic — it surfaces in the warn-once log line that fires when `POSTHOG_API_KEY` is missing, so co-located services each get one warning instead of one per call. The returned client is shared across callers; events are **not** auto-prefixed.

The `posthog` SDK is imported inside `get_posthog_client` on first successful call (when an API key is set). Consumers that never call it do not need the `[posthog]` extra installed — semantic-index relies on this.

## Telemetry

`RequestTelemetry(api_call_keys, distinct_id, event_prefix)` tracks step timings + per-service API-call counts for a single request. The `event_prefix` namespaces the emitted PostHog events: `lookup` produces `lookup_<step>` per-step events and a `lookup_completed` summary; `request` produces `request_<step>` and `request_completed`.

`track_step` is a context manager that records duration even when the body raises (it re-raises after stamping `error_type`). `send_to_posthog` reads cache stats from `cache_stats.get_cache_stats()` so telemetry and cache-recording stay decoupled — consumers initialize cache stats and telemetry independently per request.
