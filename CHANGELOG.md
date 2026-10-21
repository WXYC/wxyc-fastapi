# Changelog

All notable changes to this project will be documented in this file. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [2.1.0] - 2026-10-21

### Added
- `set_error_tag(key, value)`, exported from `wxyc_fastapi.observability`: an errors-only request tag. The pair is stored in a per-request dict in the package's own contextvar (opened by `RequestIdMiddleware` beside the breadcrumb trail) and `PosthogErrorsClient` merges it into `$exception` events only, on both the exception and message-only paths. The contract fields (`service`, `environment`, `release`, `$process_person_profile`, `request_id`) are applied after the tags, and a tag named like one of them, `breadcrumbs`, `logger` or any `$`-prefixed key is ignored, so a tag cannot override or fill in a contract field or a property the call passes in. Values are cut to 200 characters; outside a request the call is dropped with one warning per process. Consumer impact: none until adopted; it replaces `posthog.contexts.tag` for tags that must not leak into analytics events.
- `normalize_environment(value) -> str`, exported from `wxyc_fastapi.observability`: the environment alias table `init_error_tracking` uses for the `environment` tag (`prod`/`stage`/`dev`/`local` and case/whitespace variants map to `production`/`staging`/`development`; an unknown value passes through with one warning). Consumers can branch on `normalize_environment(env) == "staging"` for a staging `daily_ceiling` instead of copying the alias table. `_normalize_environment` remains as an alias. Consumer impact: none until adopted.
- `wxyc_fastapi.testing.isolated_error_tracking(stub=None)` and the `isolated_error_tracking_stub` pytest fixture: run a test against a fresh error-tracking singleton wired to a `CountingPosthog`, with root handlers, both excepthooks, the hooks-installed flag and the singleton restored on exit. Consumers no longer need to reach into private names in wiring tests. Consumer impact: none until adopted.

### Changed
- `get_posthog_errors_client` registers `shutdown_posthog_errors` with `atexit` each time it builds the client (unregistering any earlier registration, so one is live and it always runs before the current SDK client's own join hook), so `$exception` events still queued at SIGTERM are flushed without each consumer wiring a shutdown. `atexit` runs after uvicorn's lifespan teardown, so ERRORs logged by close steps are still delivered. `shutdown_posthog_errors` stays public and idempotent; a client rebuilt after an explicit shutdown gets the hook re-registered after its SDK hook. Consumer impact: none required; a consumer's explicit call is harmless, but one placed before its other close steps drops their ERRORs and can be removed.
- `add_breadcrumb` now records a real trail: each call appends `{category, message, level, data, timestamp}` to a per-request list in the package's own contextvar (opened by `RequestIdMiddleware`; last 50 kept, message and data size-bounded), and `PosthogErrorsClient` attaches it to `$exception` events only as the `breadcrumbs` property. It no longer calls `posthog.contexts.tag`, so breadcrumbs stop leaking into analytics events captured in the same request, `data` and `level` are kept, and two breadcrumbs of one category no longer overwrite each other. Outside a request a breadcrumb is still dropped. The `breadcrumb.<category>` event properties are gone; read `breadcrumbs` instead.

### Consumer impact
- Any dashboard, insight or query that read `breadcrumb.<category>` properties off analytics or exception events must read the `breadcrumbs` list on `$exception` events instead; the `breadcrumb.<category>` properties are no longer written. A breadcrumb `message` is now truncated to 1000 characters, and a `data` whose `repr` exceeds 1000 characters is stored as its truncated `repr` string. `data` is snapshotted at call time, so later mutation by the caller does not change the recorded crumb.
- Why this is a minor (2.1.0) and not a major, although the `breadcrumb.<category>` properties are removed: no code API changed. `add_breadcrumb` keeps its signature and import path, every other public name is unchanged or new, and no consumer code stops importing, type-checking or running. What changed is the event properties the package writes, which are telemetry output and not part of the Python API that SemVer versions here. Consumers move their pin from `>=2.0.0,<3.0.0` to `>=2.1.0,<3.0.0` only when they adopt `set_error_tag`, `normalize_environment` or `isolated_error_tracking`.

[2.1.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v2.1.0

## [2.0.0] - 2026-10-19

### Removed
- **Breaking:** `init_sentry`, `_default_integrations` and the `sentry-sdk[fastapi]` dependency. `wxyc_fastapi.observability.sentry` is deleted, so `from wxyc_fastapi.observability.sentry import ...` and `from wxyc_fastapi.observability import init_sentry` raise `ImportError`. Use `init_error_tracking` (added in 1.6.0). The `sentry` package keyword and the "Sentry init" wording in the package description are gone too.

### Changed
- `capture_exception` and `add_breadcrumb` moved from `observability/sentry.py` to `observability/errors.py`. They are still exported from `wxyc_fastapi.observability` under the same names and behave as in 1.6.0 (PostHog-routed). Only a deep import of `wxyc_fastapi.observability.sentry` breaks.

### Consumer impact
- A consumer that still calls `init_sentry` must move to `init_error_tracking` before raising its pin to 2.x; the known consumers pin `<2.0.0`, so nothing breaks until they bump. A consumer that relied on `sentry-sdk` arriving transitively through `wxyc-fastapi` must declare it itself. Not tagged in this change: publishing is a separate step that tags the merge commit.

## [1.6.0] - 2026-10-19

### Added
- `wxyc_fastapi.observability.errors` — PostHog error reporting to replace Sentry's `LoggingIntegration`. `get_posthog_errors_client()` returns a singleton keyed by `POSTHOG_ERRORS_API_KEY` (separate from the analytics `POSTHOG_API_KEY`; `None` when unset; frame locals off), and `PosthogErrorsHandler` is a `logging.Handler` for ERROR and above. Records with `exc_info` go through the SDK's `capture_exception`; message-only records (which `capture_exception` silently drops) are sent as a synthesized `$exception` event with a one-entry `$exception_list` (`mechanism = {type: "generic", handled: true, synthetic: true}`, type `Message`, a `logger` property) and `$exception_fingerprint = "<logger name>:<unformatted msg>"` (one string; PostHog types the field `Option<String>` and drops events that send a list), so occurrences group by call site. Both paths sit behind a token bucket (`ERRORS_BURST` capacity, refilled at `daily_ceiling / 86400` per second) and a same-fingerprint dedupe window (`ERRORS_DEDUPE_WINDOW_SECONDS`), because the PostHog free plan caps exceptions at 100K/month. Drops log one WARNING at most once a minute with the count. The handler ignores records from the `posthog` and `wxyc_fastapi.observability` logger trees (the rest of the package, e.g. `wxyc_fastapi.healthcheck`, is reported) and guards against same-thread re-entrancy.
- `init_error_tracking(*, service, environment, daily_ceiling, release=None, before_send=None, app=None)` (`observability/error_tracking.py`) — the one call that replaces `init_sentry`'s error path. It builds the errors client, attaches `PosthogErrorsHandler` to the root logger (once), chains `sys.excepthook` / `threading.excepthook` for exception autocapture (the SDK's own autocapture stays off because it would bypass the client's dedupe and token bucket), and, when `app` is given, registers an `Exception` handler that reports through the same client and returns a plain 500. `environment` is normalized to `production` / `staging` / `development` (`prod`, `stage`, `dev`, `local` are aliases; an unknown value passes through with one warning). `release` is stamped on every event when given; `before_send` is forwarded to the SDK. One failure is one event in any order (log then raise, `capture_exception` then raise, or raise alone): every capture path marks the exception once the gate admits it and skips marked ones. With `app`, `RequestIdMiddleware` reads a valid `X-Request-ID` (1-128 of `[A-Za-z0-9._-]`) or generates one, echoes it on the response, and events inside the request carry `request_id`. Uncaught crashes are sent with `mechanism {type: excepthook | threading.excepthook | starlette, handled: false}` (SDK 7.65.0 and later). Exception events carry a `logger` property. Known limit: after `shutdown_posthog_errors`, a second `init_error_tracking` leaves the root handler and the excepthooks bound to the first client. `init_sentry` is unchanged, so a consumer can run both during a parity window.
- `get_current_errors_client()`, `is_reported()` and `mark_reported()` in `observability/errors.py`; `PosthogErrorsClient.capture_exception(..., properties=...)` and `PosthogErrorsClient(release=...)`.

### Changed
- `RequestIdMiddleware` runs each HTTP request inside `posthog.contexts.new_context(fresh=True, capture_exceptions=False)` when the `posthog` extra is installed, so `add_breadcrumb` and `contexts.tag` land on that request's events without the consumer opening a context, and nothing leaks between requests. `capture_exceptions=False` keeps the SDK from capturing route exceptions itself, outside the client's dedupe window and token bucket. A route crash is reported from inside that context, so its event carries the request's breadcrumbs and tags; the Starlette 500 handler, which runs outside user middleware, then skips it as already reported. Without the extra the middleware is unchanged.
- `capture_exception(err, context, context_name=)` and `add_breadcrumb(category=, ...)` (`observability/sentry.py`) now report through the PostHog errors client instead of Sentry. `capture_exception` attaches `context` as the `context_name` event property and is a no-op until `init_error_tracking` has run. `add_breadcrumb` tags the current `posthog.contexts` context with `breadcrumb.<category>` = `message` and is dropped when no context is active (the middleware opens one per request); `data` and `level` are accepted but not recorded.
- `daily_ceiling` is now a required keyword of `get_posthog_errors_client` and `PosthogErrorsClient` (the `ERRORS_DAILY_CEILING` constant is gone): the contract's budgets differ per service (LML prod 300, request-o-matic and semantic-index 100, each staging process 50).
- `PosthogErrorsClient.capture_exception` / `capture_message` swallow SDK errors (logged at WARNING) so callers other than the logging handler never see them.
- `get_posthog_errors_client` is first-call-wins: a later call with a different `service`, `environment` or `daily_ceiling` logs one warning that its values are ignored.
- An unreadable or unparseable `posthog` version now sets `$exception_level` (it used to skip it), and pre-release versions (`7.65.0rc1`) are read by their leading integers.
- The `posthog` extra's floor rises from `>=3.0` to `>=7.65.0` (the `dev` extra moves with it): 7.65.0 is the first release that sets `$exception_level` itself and honors the `handled: false` mechanism the excepthook and Starlette crash paths send, so those crashes are no longer reported as handled. The pre-7.65 fallback branches in `errors.py` stay as a defensive path for an unreadable SDK version.

### Consumer impact
- A consumer that calls `capture_exception` / `add_breadcrumb` and has not called `init_error_tracking` stops reporting to Sentry through those shims and reports nothing until it does; call `init_error_tracking` (alongside `init_sentry` during a parity window) before upgrading. A direct caller of `get_posthog_errors_client` or `PosthogErrorsClient` must now pass `daily_ceiling`. A consumer pinning `posthog<7.65.0` must raise its pin to install the `[posthog]` extra (the known consumers pin 7.x). Not tagged in this change: publishing is a separate step that tags the merge commit.

## [1.5.0] - 2026-08-10

Adds a `before_send_transaction` passthrough to `init_sentry`, symmetric with the existing `before_send`. Motivated by the WXYC Sentry org crossing 80% of its monthly reserved span budget on 2026-08-10: spans and errors sit in separate quota buckets, so `traces_sample_rate=1.0` (unchanged by this release — see below) keeps every service tracing at full fidelity, and `before_send_transaction` is the only hook available to shed noise at the span level rather than the sample-rate level. Concretely, this unblocks LML dropping asyncpg's connection-reset statement (`SELECT pg_advisory_unlock_all(); CLOSE ALL; UNLISTEN *; RESET ALL;`), which alone accounted for 1,528,070 spans in 14 days — 19% of the org's total span consumption — with one fixed, sub-millisecond, pool-bookkeeping description.

### Added
- `init_sentry(..., before_send_transaction=None)` — optional, keyword-only callable forwarded to `sentry_sdk.init`. Fires once per transaction envelope. Mutate `event["spans"]` and return the modified `Event` to drop individual child spans while keeping the transaction; return `None` to drop the entire transaction, including all of its spans — a much blunter operation, and the key distinction from `before_send`, which only ever sees error events, never transactions.
- `TestInitSentry.test_before_send_transaction_passed_through` / `test_before_send_transaction_defaults_to_none` (`tests/observability/test_sentry.py`) — mirror the existing `before_send` coverage: the callable reaches `sentry_sdk.init` unchanged, and omitting it preserves `None`.

### Decided
- `traces_sample_rate` keeps its `1.0` default in this release. Lowering it would alter the telemetry of every consumer at once, including LML's `lookup/router.py` measurement series, which explicitly assumes full-fidelity traces. A lower default needs its own PR with a CHANGELOG breaking-change note and a named consumer fan-out, not a silent bundle with this passthrough.

### Consumer impact
- Additive and backward-compatible (minor bump). `before_send_transaction` defaults to `None`, so every existing call site is byte-identical with no code change. A consumer re-pins `>=1.4.0,<2.0.0` → `>=1.5.0,<2.0.0` only when it passes `before_send_transaction=`. First consumer: library-metadata-lookup, filtering the asyncpg reset-span noise.

[1.5.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v1.5.0

## [1.4.0] - 2026-08-09

Adds a process-wide capture rate limiter to the PostHog wrapper, plus consumer-facing capture-budget test helpers. Both are the structural follow-through on the 2026-08-04 incident that 1.3.0's `emit_step_events` toggle mitigated case-by-case: LML's per-step `lookup_*` events (emitted into the "Request-O-Matic" PostHog project via a shared `POSTHOG_API_KEY`) reached ≈434k events/day during a Backend-Service bulk campaign, exhausted the org-wide PostHog product-analytics event quota, and ingestion for every WXYC project cut off at 2026-08-05 00:46 UTC. The toggle removed that one flood; the limiter bounds the next one no matter which capture site causes it, and the budget helpers let consumer tests pin per-operation event counts so the next flood never ships.

### Added
- Capture rate limiter in `get_posthog_client` — the returned client is now a token-bucket proxy around the SDK client (`POSTHOG_RATELIMIT_EVENTS_PER_MINUTE`, default 60 sustained; `POSTHOG_RATELIMIT_BURST`, default 1800 bucket capacity; `0` disables wrapping and returns the raw client; env is read once, at first construction). Env parsing rejects non-finite values (`nan` would silently defeat every guard — either dropping 100% of captures or disabling the limiter entirely depending on which knob it lands in — and `inf` would pin the bucket full) and clamps a burst in (0, 1) to 1 (any such burst would permanently drop every capture, since admission requires a whole token). Only explicit `capture` calls are gated — `flush`, `shutdown`, feature-flag reads, and `capture_exception` delegate untouched (exceptions bill under PostHog error tracking's separate quota); attribute writes (e.g. the SDK's `client.disabled = True` kill switch) forward to the inner client rather than shadowing on the proxy. Dropped captures batch into at most one `telemetry_rate_limited` event per minute carrying `dropped_count`, `sample_events` (≤5 distinct names), `limit_events_per_minute`, `burst`, and `trigger_distinct_id`, emitted under the stable identity `wxyc-fastapi-ratelimiter` so alerts can key on it; the first drop after a quiet stretch reports immediately, a pending tail is drained by the next admitted capture past the interval and force-drained by `flush`/`shutdown` (a flood that simply ends is still accounted for), and every report also logs at WARNING. Admission is lock-protected and per-process: an N-worker deploy admits N× the configured rate.
- `POSTHOG_RATELIMIT_EXEMPT_EVENTS` — comma-separated exact event names that bypass the bucket, for hand-picked, inherently low-rate counters that feed alerts and must survive exactly the saturation windows that trip the limiter (e.g. LML's `discogs_rate_gate_fail_open`). An exempt event is unbounded again; keep the list short.
- `wxyc_fastapi.testing` — `CountingPosthog` (in-memory client stand-in recording `CapturedEvent(event, distinct_id, properties)`, no-op `flush`/`shutdown`, `capture_exception` recorded as `$exception`, flag reads resolving `None`) and `capture_budget(max_events, client=None)`, a context manager that fails the test naming the captured events when a flow exceeds its budget, plus `as_posthog(...)` (zero-cost `typing.cast` so the fake satisfies `Posthog | None` annotations under mypy). `capture` mirrors the posthog 7.x signature the consumers pin, so the legacy two-positional `capture("uid", "event")` raises `TypeError` here — the real 7.x SDK logs-and-discards that call, and a budget test must fail loudly rather than certify telemetry production would drop. Usable without the `[posthog]` extra.
- `tests/observability/test_posthog_ratelimit.py` (admission/refill/burst, drop batching + report cadence + flush/shutdown/admitted-capture drains, exempt bypass, positional-form capture, attribute read+write delegation, shallow-copy safety, raising-`__str__` containment, report-emit failure swallowing, env parsing incl. disable switch, invalid, non-finite, and sub-1-burst fallbacks), `tests/testing/test_capture_budget.py`, and `tests/test_version.py` (pins `__version__` to `pyproject.toml` so the release-version drift fixed below cannot silently recur).

### Changed
- `src/wxyc_fastapi/__init__.py` `__version__` corrected — it had sat at a stale `1.2.0` since the 1.3.0 cut; it now tracks the release version again, and `tests/test_version.py` enforces parity.
- The posthog singleton-reset test fixture moved from a per-file copy into `tests/observability/conftest.py` (the repo's conftest convention), so both posthog test modules share one reset surface.

### Consumer impact
- Minor bump with a **default-on behavior change** (same precedent as PR 2's `HttpxIntegration` default-on flip): with no env set, a consumer's capture stream is now capped at 60/min sustained (burst 1800) per process, with drops surfaced as `telemetry_rate_limited` events and WARNING logs. Normal request-path telemetry in library-metadata-lookup and request-o-matic sits far below the ceiling; bulk campaigns that exceed it are precisely the pathology the limiter exists to bound — aggregate those to per-run summary events rather than raising the limit. Set `POSTHOG_RATELIMIT_EVENTS_PER_MINUTE=0` to restore raw-client behavior, tune both knobs per service (divide by worker count for multi-worker deploys, e.g. LML's N=3), and list alert-feeding counters that must never drop in `POSTHOG_RATELIMIT_EXEMPT_EVENTS`. Known boundary: the SDK's own `$feature_flag_called` emission happens inside the inner client and is not gated by this proxy. Consumers adopting any new surface re-pin `>=1.4.0,<2.0.0`.

[1.4.0]: https://github.com/WXYC/wxyc-fastapi/releases/tag/v1.4.0

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
