"""PostHog client wrapper.

Provides a thread-safe singleton ``Posthog`` client that lazy-initializes from
the ``POSTHOG_API_KEY`` env var. When the key is unset, ``get_posthog_client``
returns ``None`` and warns once per ``event_prefix`` so a misconfigured deploy
surfaces in logs without flooding them.

The returned client is wrapped in a process-wide capture rate limiter (token
bucket) unless disabled. A runaway instrumentation path — per-step events on a
bulk campaign, a capture inside a poll loop — is capped at
``POSTHOG_RATELIMIT_EVENTS_PER_MINUTE`` sustained (default 60) with a burst
allowance of ``POSTHOG_RATELIMIT_BURST`` tokens (default 1800), instead of
consuming the org-wide PostHog event quota: the 2026-08-04 quota cutoff took
every WXYC project's analytics dark for days. Dropped captures are counted and
surfaced as a single ``telemetry_rate_limited`` event at most once per minute,
so the suppression channel cannot itself flood. The limiter is a per-process
circuit breaker for disasters, not a budget enforcer — multi-worker deploys
multiply the effective ceiling by the worker count, and the sustained default
still admits ~86k events/day. Set ``POSTHOG_RATELIMIT_EVENTS_PER_MINUTE=0``
(or negative) to disable wrapping entirely; the raw SDK client is returned.

Only ``capture`` is rate-limited. Every other attribute (``flush``,
``shutdown``, feature-flag reads, ``capture_exception``) delegates straight to
the wrapped SDK client — exceptions are quota'd separately under PostHog error
tracking, and flag reads are not events.

The ``posthog`` SDK is an optional dependency (the ``[posthog]`` extra). The
top-level module import is free of ``posthog`` references so consumers like
semantic-index — which never call ``get_posthog_client`` — can import
``wxyc_fastapi.observability`` without installing the extra.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Callable

    from posthog import Posthog

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "https://us.i.posthog.com"

_DEFAULT_EVENTS_PER_MINUTE = 60.0
_DEFAULT_BURST = 1800.0
_REPORT_EVENT = "telemetry_rate_limited"
_REPORT_INTERVAL_SECONDS = 60.0
_SAMPLE_LIMIT = 5

_client: Posthog | None = None
_warned_prefixes: set[str] = set()
_lock = threading.Lock()


def _capture_event_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Best-effort event name from a ``capture(...)`` call, for drop samples.

    All in-org call sites use the kwargs form; the positional fallbacks cover
    the SDK's ``capture(distinct_id, event, ...)`` legacy signature.
    """
    event = kwargs.get("event")
    if event is None and len(args) >= 2:
        event = args[1]
    elif event is None and args:
        event = args[0]
    return str(event) if event is not None else "<unknown>"


class _RateLimitedPosthog:
    """Token-bucket rate limiter in front of a ``Posthog`` client's ``capture``.

    Admission is decided per process under a lock; the forwarded ``capture``
    itself runs outside the lock so a slow SDK enqueue never serializes other
    captures. Drops are batched into one ``telemetry_rate_limited`` report per
    ``_REPORT_INTERVAL_SECONDS`` carrying the dropped count and a sample of up
    to ``_SAMPLE_LIMIT`` distinct dropped event names — the first drop after a
    quiet stretch reports immediately so a flood is visible within seconds.
    The report bypasses the bucket (it is the accounting for dropped events,
    bounded by the report interval) and swallows its own emit failures: meta-
    telemetry must never raise where the suppressed capture would not have.

    Every attribute other than ``capture`` delegates to the wrapped client, so
    the proxy is a drop-in ``Posthog`` for consumers (feature flags, ``flush``,
    ``shutdown``).
    """

    def __init__(
        self,
        inner: Posthog,
        *,
        events_per_minute: float,
        burst: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._events_per_minute = float(events_per_minute)
        self._burst = float(burst)
        self._clock = clock
        self._bucket_lock = threading.Lock()
        self._tokens = self._burst
        self._last_refill = clock()
        self._dropped_since_report = 0
        self._dropped_samples: list[str] = []
        # None sentinel: the very first drop reports immediately rather than
        # waiting out a full interval with the operator blind.
        self._last_report_at: float | None = None

    @property
    def events_per_minute(self) -> float:
        """Sustained admission rate."""
        return self._events_per_minute

    @property
    def burst(self) -> float:
        """Bucket capacity: captures admitted at once after a quiet stretch."""
        return self._burst

    def capture(self, *args: Any, **kwargs: Any) -> Any:
        now = self._clock()
        report: dict[str, Any] | None = None
        with self._bucket_lock:
            rate_per_second = self._events_per_minute / 60.0
            self._tokens = min(
                self._burst, self._tokens + (now - self._last_refill) * rate_per_second
            )
            self._last_refill = now
            admitted = self._tokens >= 1.0
            if admitted:
                self._tokens -= 1.0
            else:
                self._dropped_since_report += 1
                name = _capture_event_name(args, kwargs)
                if len(self._dropped_samples) < _SAMPLE_LIMIT and name not in self._dropped_samples:
                    self._dropped_samples.append(name)
                due = (
                    self._last_report_at is None
                    or (now - self._last_report_at) >= _REPORT_INTERVAL_SECONDS
                )
                if due:
                    report = {
                        "dropped_count": self._dropped_since_report,
                        "sample_events": list(self._dropped_samples),
                        "limit_events_per_minute": self._events_per_minute,
                        "burst": self._burst,
                    }
                    self._last_report_at = now
                    self._dropped_since_report = 0
                    self._dropped_samples = []
        if admitted:
            return self._inner.capture(*args, **kwargs)
        if report is not None:
            logger.warning(
                "PostHog capture rate limit hit: dropped %d event(s) since last report (sample: %s)",
                report["dropped_count"],
                report["sample_events"],
            )
            try:
                self._inner.capture(
                    distinct_id=kwargs.get("distinct_id") or "wxyc-fastapi",
                    event=_REPORT_EVENT,
                    properties=report,
                )
            except Exception:
                logger.warning("Failed to emit %s report", _REPORT_EVENT, exc_info=True)
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _read_float_env(var: str, default: float) -> float:
    """``float(os.environ[var])`` with warn-and-default on unset/invalid."""
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", var, raw, default)
        return default


def _rate_limit_config() -> tuple[float, float] | None:
    """``(events_per_minute, burst)``, or ``None`` when rate limiting is disabled.

    ``POSTHOG_RATELIMIT_EVENTS_PER_MINUTE <= 0`` is the explicit off switch. A
    non-positive burst has no such meaning — it would make every capture drop —
    so it falls back to the default instead.
    """
    events_per_minute = _read_float_env(
        "POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", _DEFAULT_EVENTS_PER_MINUTE
    )
    if events_per_minute <= 0:
        return None
    burst = _read_float_env("POSTHOG_RATELIMIT_BURST", _DEFAULT_BURST)
    if burst <= 0:
        logger.warning("Non-positive POSTHOG_RATELIMIT_BURST; using default %s", _DEFAULT_BURST)
        burst = _DEFAULT_BURST
    return events_per_minute, burst


def get_posthog_client(event_prefix: str) -> Posthog | None:
    """Return the singleton ``Posthog`` client, or ``None`` when unconfigured.

    On first call with a missing/empty ``POSTHOG_API_KEY``, logs a single
    warning that names ``event_prefix`` so the misconfigured caller is visible.
    Subsequent calls with the same ``event_prefix`` are silent; calls with a new
    ``event_prefix`` warn again so co-located services each get one warning.

    Unless ``POSTHOG_RATELIMIT_EVENTS_PER_MINUTE`` disables it, the returned
    client is the capture rate-limiting proxy described in the module
    docstring; rate-limit env vars are read once, when the singleton is first
    constructed.

    Args:
        event_prefix: Caller's event-name prefix (e.g. ``"request"``,
            ``"lookup"``). Used for diagnostic logging only — the returned
            client is not auto-prefixing events.

    Returns:
        The shared client, or ``None`` if no API key is set.
    """
    global _client
    with _lock:
        api_key = os.environ.get("POSTHOG_API_KEY")
        if not api_key:
            if event_prefix not in _warned_prefixes:
                _warned_prefixes.add(event_prefix)
                logger.warning(
                    "POSTHOG_API_KEY not set; PostHog telemetry disabled (caller=%s)",
                    event_prefix,
                )
            return None
        if _client is None:
            from posthog import Posthog  # noqa: PLC0415  lazy: only needed when key is set

            host = os.environ.get("POSTHOG_HOST", _DEFAULT_HOST)
            raw_client = Posthog(api_key, host=host)
            config = _rate_limit_config()
            if config is None:
                _client = raw_client
            else:
                events_per_minute, burst = config
                # The proxy is duck-type-complete (``capture`` gated, all other
                # attributes delegated), so the declared ``Posthog`` return
                # type stays truthful for consumers.
                _client = cast(
                    "Posthog",
                    _RateLimitedPosthog(
                        raw_client, events_per_minute=events_per_minute, burst=burst
                    ),
                )
        return _client


def flush_posthog() -> None:
    """Flush pending PostHog events. Safe no-op when no client is initialized."""
    if _client is not None:
        _client.flush()


def shutdown_posthog() -> None:
    """Shut down the PostHog client and clear the singleton.

    Safe no-op when no client is initialized. Note that ``_warned_prefixes`` is
    intentionally **not** cleared — the warn-once contract is per-process, not
    per-client-lifecycle, so a process that shuts down and re-initializes a
    client will not re-warn for the same caller.
    """
    global _client
    if _client is not None:
        _client.shutdown()
        _client = None
