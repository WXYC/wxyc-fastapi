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
surfaced as a single ``telemetry_rate_limited`` event at most once per minute
(also drained by ``flush``/``shutdown`` and by the first admitted capture past
the interval, so a flood's tail is accounted for), and the suppression channel
cannot itself flood. Event names listed in ``POSTHOG_RATELIMIT_EXEMPT_EVENTS``
(comma-separated, exact names) bypass the bucket entirely — reserve this for
hand-picked, inherently low-rate counters that feed alerts and must survive
exactly the saturation windows that trip the limiter; an exempt event is
unbounded again. The limiter is a per-process circuit breaker for disasters,
not a budget enforcer — multi-worker deploys multiply the effective ceiling by
the worker count, and the sustained default still admits ~86k events/day. Set
``POSTHOG_RATELIMIT_EVENTS_PER_MINUTE=0`` (or negative) to disable wrapping
entirely; the raw SDK client is returned.

Only explicit ``capture`` calls are rate-limited. ``flush``, ``shutdown``,
feature-flag reads, and ``capture_exception`` delegate to the wrapped SDK
client — exceptions are quota'd separately under PostHog error tracking. Note
that flag reads are NOT free of billable events: the SDK's own
``$feature_flag_called`` emission happens inside the inner client, below this
proxy, and is not gated (gating every SDK-internal emission path needs a
``before_send`` hook — tracked separately).

The ``posthog`` SDK is an optional dependency (the ``[posthog]`` extra). The
top-level module import is free of ``posthog`` references so consumers like
semantic-index — which never call ``get_posthog_client`` — can import
``wxyc_fastapi.observability`` without installing the extra.
"""

from __future__ import annotations

import logging
import math
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
_REPORT_DISTINCT_ID = "wxyc-fastapi-ratelimiter"
_REPORT_INTERVAL_SECONDS = 60.0
_SAMPLE_LIMIT = 5

# Attributes that live on the proxy itself; every other attribute read AND
# write delegates to the wrapped client (a write like the SDK's documented
# ``client.disabled = True`` kill switch must reach the object that checks it,
# not shadow it on the proxy).
_PROXY_ATTRS = frozenset(
    {
        "_inner",
        "_events_per_minute",
        "_burst",
        "_clock",
        "_exempt_events",
        "_bucket_lock",
        "_tokens",
        "_last_refill",
        "_dropped_since_report",
        "_dropped_samples",
        "_last_report_at",
    }
)

_client: Posthog | None = None
_warned_prefixes: set[str] = set()
_lock = threading.Lock()


def _capture_event_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Best-effort event name from a ``capture(...)`` call, for exempt checks
    and drop samples.

    All in-org call sites use the kwargs form; the positional fallbacks cover
    the SDK's legacy ``capture(distinct_id, event, ...)`` signature. Wrapped in
    ``_safe_capture_event_name`` because ``str()`` runs caller-supplied
    ``__str__`` — a raising one must not escape ``capture``.
    """
    event = kwargs.get("event")
    if event is None and len(args) >= 2:
        event = args[1]
    elif event is None and args:
        event = args[0]
    return str(event) if event is not None else "<unknown>"


def _safe_capture_event_name(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        return _capture_event_name(args, kwargs)
    except Exception:
        return "<unrepresentable>"


class _RateLimitedPosthog:
    """Token-bucket rate limiter in front of a ``Posthog`` client's ``capture``.

    Admission is decided per process under a lock (the clock is read inside it
    so a preempted thread cannot apply a stale timestamp and spuriously debit
    tokens); the forwarded ``capture`` itself runs outside the lock so a slow
    SDK enqueue never serializes other captures. Drops are batched into one
    ``telemetry_rate_limited`` report per ``_REPORT_INTERVAL_SECONDS``, carrying
    the dropped count and a sample of up to ``_SAMPLE_LIMIT`` distinct dropped
    event names. The first drop after a quiet stretch reports immediately; a
    pending tail is drained by the next admitted capture past the interval and
    force-drained by ``flush``/``shutdown``, so a flood that simply ends still
    gets accounted for. Reports use a stable identity (``_REPORT_DISTINCT_ID``,
    with the triggering capture's distinct_id carried in properties) so alerts
    can key on them; they bypass the bucket (bounded by the report interval)
    and swallow their own emit failures — meta-telemetry must never raise where
    the suppressed capture would not have.

    Attribute reads and writes other than the gated surface delegate to the
    wrapped client, so the proxy is a drop-in ``Posthog`` for consumers
    (feature flags, ``disabled``, ``debug``).
    """

    def __init__(
        self,
        inner: Posthog,
        *,
        events_per_minute: float,
        burst: float,
        clock: Callable[[], float] = time.monotonic,
        exempt_events: frozenset[str] = frozenset(),
    ) -> None:
        self._inner = inner
        self._events_per_minute = float(events_per_minute)
        self._burst = float(burst)
        self._clock = clock
        self._exempt_events = exempt_events
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

    @property
    def exempt_events(self) -> frozenset[str]:
        """Event names that bypass the bucket entirely."""
        return self._exempt_events

    def capture(self, *args: Any, **kwargs: Any) -> Any:
        event_name = _safe_capture_event_name(args, kwargs)
        if event_name in self._exempt_events:
            return self._inner.capture(*args, **kwargs)

        report: dict[str, Any] | None = None
        with self._bucket_lock:
            now = self._clock()
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
                if (
                    len(self._dropped_samples) < _SAMPLE_LIMIT
                    and event_name not in self._dropped_samples
                ):
                    self._dropped_samples.append(event_name)
            due = (
                self._last_report_at is None
                or (now - self._last_report_at) >= _REPORT_INTERVAL_SECONDS
            )
            if self._dropped_since_report > 0 and due:
                report = self._build_report_locked(
                    now, trigger_distinct_id=kwargs.get("distinct_id")
                )

        result = self._inner.capture(*args, **kwargs) if admitted else None
        if report is not None:
            self._emit_report(report)
        return result

    def flush(self) -> Any:
        """Drain any pending drop report, then flush the wrapped client."""
        self._drain_pending_report()
        return self._inner.flush()

    def shutdown(self) -> Any:
        """Drain any pending drop report, then shut down the wrapped client."""
        self._drain_pending_report()
        return self._inner.shutdown()

    def _drain_pending_report(self) -> None:
        """Force-emit accumulated drop accounting regardless of the interval.

        Lifecycle points (flush, shutdown) must not lose a flood's tail — the
        drop counters are process memory and a redeploy discards them.
        """
        report: dict[str, Any] | None = None
        with self._bucket_lock:
            if self._dropped_since_report > 0:
                report = self._build_report_locked(self._clock(), trigger_distinct_id=None)
        if report is not None:
            self._emit_report(report)

    def _build_report_locked(self, now: float, trigger_distinct_id: Any) -> dict[str, Any]:
        """Snapshot + reset drop accounting. Caller must hold ``_bucket_lock``."""
        report = {
            "dropped_count": self._dropped_since_report,
            "sample_events": list(self._dropped_samples),
            "limit_events_per_minute": self._events_per_minute,
            "burst": self._burst,
            "trigger_distinct_id": trigger_distinct_id,
        }
        self._last_report_at = now
        self._dropped_since_report = 0
        self._dropped_samples = []
        return report

    def _emit_report(self, report: dict[str, Any]) -> None:
        logger.warning(
            "PostHog capture rate limit hit: dropped %d event(s) since last report (sample: %s)",
            report["dropped_count"],
            report["sample_events"],
        )
        try:
            self._inner.capture(
                distinct_id=_REPORT_DISTINCT_ID,
                event=_REPORT_EVENT,
                properties=report,
            )
        except Exception:
            logger.warning("Failed to emit %s report", _REPORT_EVENT, exc_info=True)

    def __getattr__(self, name: str) -> Any:
        # ``__dict__.get`` (not ``self._inner``) so object-copy protocols that
        # build instances without __init__ get AttributeError, not recursion.
        inner = self.__dict__.get("_inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _PROXY_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(self._inner, name, value)


def _read_float_env(var: str, default: float) -> float:
    """``float(os.environ[var])`` with warn-and-default on unset/invalid.

    Non-finite values are invalid: ``nan`` defeats every ``<=``/``>=`` guard
    (silently disabling the limiter or silently dropping every capture,
    depending on which knob it lands in) and ``inf`` pins the bucket full.
    """
    raw = os.environ.get(var)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", var, raw, default)
        return default
    if not math.isfinite(value):
        logger.warning("Non-finite %s=%r; using default %s", var, raw, default)
        return default
    return value


def _rate_limit_config() -> tuple[float, float, frozenset[str]] | None:
    """``(events_per_minute, burst, exempt_events)``, or ``None`` when disabled.

    ``POSTHOG_RATELIMIT_EVENTS_PER_MINUTE <= 0`` is the explicit off switch. A
    burst below 1 has no such meaning — tokens are clamped to ``min(burst, …)``
    while admission requires ``>= 1.0``, so any burst in (0, 1) would drop
    every capture forever; non-positive falls back to the default and (0, 1)
    clamps up to 1.
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
    elif burst < 1:
        logger.warning("POSTHOG_RATELIMIT_BURST=%s is below 1; clamping to 1", burst)
        burst = 1.0
    raw_exempt = os.environ.get("POSTHOG_RATELIMIT_EXEMPT_EVENTS", "")
    exempt_events = frozenset(name.strip() for name in raw_exempt.split(",") if name.strip())
    return events_per_minute, burst, exempt_events


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
                events_per_minute, burst, exempt_events = config
                # The proxy delegates all non-gated reads and writes, so the
                # declared ``Posthog`` return type stays truthful for consumers.
                _client = cast(
                    "Posthog",
                    _RateLimitedPosthog(
                        raw_client,
                        events_per_minute=events_per_minute,
                        burst=burst,
                        exempt_events=exempt_events,
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
