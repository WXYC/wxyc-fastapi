# Migration playbook (for consumer-side tickets A2/A3/A4)

1. Add `wxyc-fastapi[posthog]>=0.1,<0.2` to `pyproject.toml`.
2. Replace `core/sentry.py` with `from wxyc_fastapi.observability import init_sentry, add_breadcrumb, capture_exception`. The breadcrumb call sites need to pass `category=` explicitly (existing rom/LML hard-coded `"discogs"`).
3. Replace `core/telemetry.py` `RequestTelemetry()` constructor sites with the parameterized version (see table above).
4. Replace cache-stats `record_*` free functions with `get_cache_stats_recorder().record_*` method calls. The ContextVar location does not change (per-request stats still live in the per-context dict).
5. Replace ad-hoc PostHog client construction with `get_posthog_client(event_prefix)`.
6. Delete the old local `core/sentry.py` and `core/telemetry.py` files.
7. Run the consumer's existing test suite — no behavior changes are expected except the `HttpxIntegration` default-on flip (LML only).
