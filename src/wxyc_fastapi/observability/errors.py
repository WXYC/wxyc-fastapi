"""PostHog error reporting: an errors-only client and an ERROR-to-exception logging handler.

``get_posthog_errors_client`` is keyed by ``POSTHOG_ERRORS_API_KEY``, separate
from the analytics ``POSTHOG_API_KEY`` so a service's errors can land in a
different PostHog project than its analytics. With the key unset it returns
``None`` and ``PosthogErrorsHandler`` is never given a client to forward to
(fail-open), and so does a missing ``posthog`` extra or a failing SDK
constructor: one warning, the failure is cached, ``None`` is returned. Frame locals (``capture_exception_code_variables``) stay off.

``PosthogErrorsHandler`` stands in for Sentry's ``LoggingIntegration``: every
record at ERROR or above is reported. Records carrying ``exc_info`` go through
the SDK's ``capture_exception``; message-only records — which the SDK's
``capture_exception`` silently drops because there is no active exception — are
sent as a synthesized ``$exception`` event (type ``Message``, a synthetic generic
mechanism, a ``logger`` property, level ``error``) whose ``$exception_fingerprint`` is
``"<logger name>:<unformatted msg>"``, so occurrences group by call site rather
than by interpolated values. PostHog types the fingerprint as a single string;
an event carrying a list is dropped at ingestion.

Every event carries ``service``, ``environment`` and ``$process_person_profile:
false`` (plus ``release`` when known), and ``distinct_id`` is the service name.
A send that raises is swallowed (fail-open), so no caller sees an SDK error.

``capture_exception`` and ``add_breadcrumb`` are the call-site shims: the first reports
through this client (a no-op until ``init_error_tracking`` has run), the second appends
to the current request's breadcrumb trail (attached to ``$exception`` events only).
``daily_ceiling`` has no default: the contract's sizes differ per service. The first
``get_posthog_errors_client`` call wins; a later call with different values logs one
warning that they are ignored.

PostHog's free plan caps exceptions at 100K/month, so the client gates both
paths behind a same-fingerprint dedupe window (``ERRORS_DEDUPE_WINDOW_SECONDS``)
and then a token bucket (``ERRORS_BURST`` capacity, refilled at
``daily_ceiling / 86400`` tokens per second). Like the analytics limiter this is a
per-process circuit breaker, not a budget enforcer: N workers admit N× the rate.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from wxyc_fastapi.observability.posthog import _DEFAULT_HOST

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import TracebackType

    from posthog import Posthog

    ExcInfo = tuple[type[BaseException], BaseException, TracebackType | None]

logger = logging.getLogger(__name__)

ERRORS_BURST = 20.0
ERRORS_DEDUPE_WINDOW_SECONDS = 60.0

_MAX_TRACKED_FINGERPRINTS = 1000
_DROP_LOG_INTERVAL_SECONDS = 60.0
# Logger trees whose output must never be re-reported: the SDK's own
# diagnostics (its consumer thread logs failed sends) and this client's module
# tree, which could log while a capture is in flight. The rest of the package
# (e.g. ``wxyc_fastapi.healthcheck``) reports real errors and is captured.
_SELF_LOGGER_TREES = ("posthog", "wxyc_fastapi.observability")
_SECONDS_PER_DAY = 86400.0
# posthog-python 7.65.0 sets ``$exception_level`` itself and warns on an override.
_SDK_SETS_LEVEL_FROM = (7, 65, 0)
# Set on an exception once a capture of it was admitted and issued, on any path.
_REPORTED_ATTR = "_wxyc_error_reported"

# The current request's id, set by the request-id middleware.
request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "wxyc_request_id", default=None
)

_client: PosthogErrorsClient | None = None
_config: tuple[str, str, float] | None = None
_warned_conflicts: set[tuple[str, str, float]] = set()
_init_failed = False
_lock = threading.Lock()


class PosthogErrorsClient:
    """Rate-limited, deduplicating front for a ``Posthog`` client's exception capture.

    Admission (dedupe first, so suppressed repeats cost no token; then the
    bucket) is decided under a lock; the forwarded SDK call runs outside it.
    """

    def __init__(
        self,
        inner: Posthog,
        *,
        service: str,
        environment: str,
        daily_ceiling: float,
        release: str | None = None,
        burst: float = ERRORS_BURST,
        dedupe_window_seconds: float = ERRORS_DEDUPE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._service = service
        self._base_properties: dict[str, Any] = {
            "service": service,
            "environment": environment,
            "$process_person_profile": False,
        }
        if release:
            self._base_properties["release"] = release
        self._sdk_omits_level = _sdk_omits_exception_level()
        self._rate_per_second = daily_ceiling / _SECONDS_PER_DAY
        self._burst = burst
        self._dedupe_window = dedupe_window_seconds
        self._clock = clock
        self._gate_lock = threading.Lock()
        self._tokens = burst
        self._last_refill = clock()
        self._seen: dict[str, float] = {}
        self._dropped = 0
        self._last_drop_log: float | None = None

    def capture_exception(
        self,
        exc_info: ExcInfo,
        *,
        logger_name: str,
        template: str,
        properties: dict[str, Any] | None = None,
        mechanism: dict[str, Any] | None = None,
        dedupe_on_message: bool = False,
    ) -> None:
        """Report an exception via the SDK; never raises. A marked exception is skipped.

        The mark is set only after the gate admits the capture. ``dedupe_on_message``
        keys the dedupe on ``type:message``; ``mechanism`` needs SDK 7.65.0+.
        """
        exc = exc_info[1]
        if is_reported(exc):
            return
        name = exc_info[0].__name__
        detail = f"{name}:{_safe_str(exc)}"[:200] if dedupe_on_message else name
        if self._admit(f"{logger_name}:{template}:{detail}"):
            merged = {**(properties or {}), "logger": logger_name, **self._event_properties()}
            if self._sdk_omits_level:
                merged["$exception_level"] = "error"
            extra = (
                {}
                if mechanism is None or self._sdk_omits_level
                else {"_capture_metadata": {"mechanism": mechanism}}
            )
            self._send(
                self._inner.capture_exception,
                exc_info,
                distinct_id=self._service,
                properties=merged,
                **extra,
            )
            # Not marked when dedupe suppresses it: the handler's unhandled event must still
            # send (matches wxyc-etl#162); the token bucket bounds the repeat.
            mark_reported(exc)

    def _event_properties(self) -> dict[str, Any]:
        request_id = request_id_var.get()
        crumbs = _breadcrumbs_var.get()
        return {
            **self._base_properties,
            **({"request_id": request_id} if request_id else {}),
            **({"breadcrumbs": list(crumbs)} if crumbs else {}),
        }

    def capture_message(self, *, logger_name: str, template: str, message: str) -> None:
        """Report a message-only record as a synthesized ``$exception`` event."""
        fingerprint = f"{logger_name}:{template}"
        if self._admit(fingerprint):
            self._send(
                self._inner.capture,
                event="$exception",
                distinct_id=self._service,
                properties={
                    **self._event_properties(),
                    "$exception_list": [
                        {
                            "type": "Message",
                            "value": message,
                            "mechanism": {"type": "generic", "handled": True, "synthetic": True},
                        }
                    ],
                    "$exception_fingerprint": fingerprint,
                    "$exception_level": "error",
                    "logger": logger_name,
                },
            )

    @staticmethod
    def _send(send: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Fail open: a raising SDK call is logged and swallowed, never propagated."""
        try:
            send(*args, **kwargs)
        except Exception:
            logger.warning("PostHog errors client failed to send; event dropped", exc_info=True)

    def flush(self) -> Any:
        """Block until the SDK's queued events are sent."""
        return self._inner.flush()

    def shutdown(self) -> Any:
        """Flush queued events and stop the SDK's consumer thread."""
        return self._inner.shutdown()

    def _admit(self, key: str) -> bool:
        with self._gate_lock:
            now = self._clock()
            admitted = self._admit_locked(key, now)
            report = None if admitted else self._count_drop_locked(now)
        if report is not None:
            logger.warning(
                "PostHog errors client dropped %d record(s) in the last %.0fs "
                "(dedupe or rate limit)",
                *report,
            )
        return admitted

    def _admit_locked(self, key: str, now: float) -> bool:
        last_seen = self._seen.get(key)
        if last_seen is not None and now - last_seen < self._dedupe_window:
            return False
        self._tokens = min(
            self._burst, self._tokens + (now - self._last_refill) * self._rate_per_second
        )
        self._last_refill = now
        if self._tokens < 1.0:
            return False
        self._tokens -= 1.0
        if len(self._seen) >= _MAX_TRACKED_FINGERPRINTS:
            self._evict_locked(now)
        self._seen[key] = now
        return True

    def _count_drop_locked(self, now: float) -> tuple[int, float] | None:
        """Count a drop; return ``(count, interval)`` when a warning is due, at most once a minute."""
        self._dropped += 1
        if (
            self._last_drop_log is not None
            and now - self._last_drop_log < _DROP_LOG_INTERVAL_SECONDS
        ):
            return None
        report = (self._dropped, _DROP_LOG_INTERVAL_SECONDS)
        self._dropped = 0
        self._last_drop_log = now
        return report

    def _evict_locked(self, now: float) -> None:
        """Drop expired entries; if none expired, drop the oldest so the table stays bounded."""
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._dedupe_window}
        if len(self._seen) >= _MAX_TRACKED_FINGERPRINTS:
            del self._seen[min(self._seen, key=self._seen.__getitem__)]


class PosthogErrorsHandler(logging.Handler):
    """Forward ERROR-and-above log records to a ``PosthogErrorsClient``.

    Never raises into the logging call site, and never reports records from the
    PostHog SDK or ``wxyc_fastapi.observability`` (nor any record logged
    re-entrantly while a capture is in flight on the same thread).
    """

    def __init__(self, client: PosthogErrorsClient, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self._errors_client = client
        self._emitting = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if _is_self_logger(record.name) or getattr(self._emitting, "active", False):
            return
        self._emitting.active = True
        try:
            template = str(record.msg)
            if record.exc_info and record.exc_info[0] is not None:
                self._errors_client.capture_exception(
                    record.exc_info, logger_name=record.name, template=template
                )
            else:
                self._errors_client.capture_message(
                    logger_name=record.name, template=template, message=record.getMessage()
                )
        except Exception:
            self.handleError(record)
        finally:
            self._emitting.active = False


def _is_self_logger(name: str) -> bool:
    return any(name == tree or name.startswith(tree + ".") for tree in _SELF_LOGGER_TREES)


def _sdk_omits_exception_level() -> bool:
    """True unless the installed ``posthog`` is known (7.65.0+) to set ``$exception_level``."""
    try:
        from posthog.version import VERSION  # noqa: PLC0415

        match = re.match(r"(\d+)\.(\d+)\.(\d+)", VERSION)
        return match is None or tuple(map(int, match.groups())) < _SDK_SETS_LEVEL_FROM
    except Exception:
        return True


def _safe_str(exc: BaseException) -> str:
    """``str(exc)``, else ``repr``, else the type name, when a hostile dunder raises."""
    try:
        return str(exc)
    except Exception:
        try:
            return repr(exc)
        except Exception:
            return type(exc).__name__


def is_reported(exc: BaseException | None) -> bool:
    """True once a capture of ``exc`` was admitted and issued."""
    return getattr(exc, _REPORTED_ATTR, False)


def mark_reported(exc: BaseException | None) -> None:
    """Mark ``exc`` as reported so every capture path skips it."""
    try:
        setattr(exc, _REPORTED_ATTR, True)
    except Exception:  # noqa: S110  an exception type refusing attributes is just re-reported
        pass


def get_current_errors_client() -> PosthogErrorsClient | None:
    """Return the already-built singleton without constructing one, or ``None``."""
    return _client


_MAX_BREADCRUMBS = 50
_MAX_BREADCRUMB_CHARS = 1000
_breadcrumbs_var: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "wxyc_fastapi_breadcrumbs", default=None
)


@contextlib.contextmanager
def request_breadcrumbs() -> Iterator[list[dict[str, Any]]]:
    """Open a fresh per-request breadcrumb trail; ``RequestIdMiddleware`` wraps each request in one."""
    trail: list[dict[str, Any]] = []
    token = _breadcrumbs_var.set(trail)
    try:
        yield trail
    finally:
        _breadcrumbs_var.reset(token)


def add_breadcrumb(
    *,
    category: str,
    message: str,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Append ``{category, message, level, data, timestamp}`` to the current request's trail.

    The category kwarg is required so each consumer keeps its own taxonomy
    (``"discogs"``, ``"groq"``, ...). The trail lives in this package's own contextvar,
    opened per request by ``RequestIdMiddleware``; it is attached to ``$exception``
    events only, as the ``breadcrumbs`` property, and never reaches analytics events.
    The last 50 entries are kept; ``message`` is truncated to 1000 characters and a
    ``data`` whose ``repr`` exceeds that becomes its truncated ``repr`` string.
    Outside a request the breadcrumb is dropped. Never raises: a ``data`` whose ``repr``
    raises is recorded as a placeholder string.
    """
    trail = _breadcrumbs_var.get()
    if trail is None:
        return
    if data is not None:
        try:
            rendered = repr(data)
        except Exception:
            data = "<unrepresentable>"  # type: ignore[assignment]
        else:
            if len(rendered) > _MAX_BREADCRUMB_CHARS:
                data = rendered[:_MAX_BREADCRUMB_CHARS]  # type: ignore[assignment]
            else:
                data = dict(data)  # snapshot: later caller mutation must not change the crumb
    trail.append(
        {
            "category": category,
            "message": str(message)[:_MAX_BREADCRUMB_CHARS],
            "level": level,
            "data": data,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        }
    )
    del trail[:-_MAX_BREADCRUMBS]


def capture_exception(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    context_name: str = "request",
) -> None:
    """Report ``error`` through the PostHog errors client, if ``init_error_tracking`` ran.

    ``context`` is attached as the ``context_name`` event property (default ``"request"``).
    """
    client = get_current_errors_client()
    if client is not None:
        client.capture_exception(
            (type(error), error, error.__traceback__),
            logger_name="capture_exception",
            template=context_name,
            dedupe_on_message=True,
            properties={context_name: context} if context else None,
        )


def get_posthog_errors_client(
    *,
    service: str,
    environment: str,
    daily_ceiling: float,
    release: str | None = None,
    before_send: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
) -> PosthogErrorsClient | None:
    """Return the singleton errors client, or ``None`` when it cannot be built.

    ``None`` means ``POSTHOG_ERRORS_API_KEY`` is unset, or the ``posthog`` import or
    constructor failed (one warning, cached). ``service`` is the ``distinct_id``;
    ``daily_ceiling`` sets the bucket refill rate; ``release`` is stamped on events;
    ``before_send`` goes to the SDK. The first call wins; a differing later call
    gets the existing client and one warning.
    """
    global _client, _config, _init_failed
    with _lock:
        api_key = os.environ.get("POSTHOG_ERRORS_API_KEY")
        if not api_key or _init_failed:
            return None
        if _client is None:
            try:
                from posthog import Posthog  # noqa: PLC0415  lazy: only needed when key is set

                host = os.environ.get("POSTHOG_HOST", _DEFAULT_HOST)
                _client = PosthogErrorsClient(
                    Posthog(
                        api_key,
                        host=host,
                        capture_exception_code_variables=False,
                        before_send=before_send,
                    ),
                    service=service,
                    environment=environment,
                    daily_ceiling=daily_ceiling,
                    release=release,
                )
                _config = (service, environment, daily_ceiling)
            except Exception:
                _init_failed = True
                logger.warning(
                    "PostHog errors client unavailable; error reporting disabled", exc_info=True
                )
                return None
        elif (requested := (service, environment, daily_ceiling)) != _config and (
            requested not in _warned_conflicts
        ):
            _warned_conflicts.add(requested)
            logger.warning(
                "PostHog errors client already configured as %s; ignored the later call's service=%r "
                "environment=%r daily_ceiling=%r",
                _config,
                service,
                environment,
                daily_ceiling,
            )
        return _client


def shutdown_posthog_errors() -> None:
    """Shut down the errors client and clear the singleton. Safe no-op when uninitialized."""
    global _client, _config, _init_failed
    _init_failed = False
    _config = None
    if _client is not None:
        _client.shutdown()
        _client = None
