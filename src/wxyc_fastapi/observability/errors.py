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
A send that raises is swallowed (fail-open) inside the client, so callers other
than the logging handler (the FastAPI handler, the ``capture_exception`` shim)
never see an SDK error. ``daily_ceiling`` has no default: the contract's sizes
differ per service. The first ``get_posthog_errors_client`` call wins; a later
call with different ``service``, ``environment`` or ``daily_ceiling`` logs one
warning that its values are ignored.

PostHog's free plan caps exceptions at 100K/month, so the client gates both
paths behind a same-fingerprint dedupe window (``ERRORS_DEDUPE_WINDOW_SECONDS``)
and then a token bucket (``ERRORS_BURST`` capacity, refilled at
``daily_ceiling / 86400`` tokens per second). Like the analytics limiter this is a
per-process circuit breaker, not a budget enforcer: N workers admit N× the rate.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import TYPE_CHECKING, Any

from wxyc_fastapi.observability.posthog import _DEFAULT_HOST

if TYPE_CHECKING:
    from collections.abc import Callable
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
# Set on an exception once the FastAPI handler has reported it, so the server's
# own ERROR log of the same re-raised exception is not reported a second time.
_REPORTED_ATTR = "_wxyc_error_reported"

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
    ) -> None:
        """Report an exception via the SDK's ``capture_exception``; never raises."""
        if self._admit(":".join((logger_name, template, exc_info[0].__name__))):
            merged = {**(properties or {}), **self._base_properties}
            if self._sdk_omits_level:
                merged["$exception_level"] = "error"
            self._send(
                self._inner.capture_exception,
                exc_info,
                distinct_id=self._service,
                properties=merged,
            )

    def capture_message(self, *, logger_name: str, template: str, message: str) -> None:
        """Report a message-only record as a synthesized ``$exception`` event."""
        fingerprint = f"{logger_name}:{template}"
        if self._admit(fingerprint):
            self._send(
                self._inner.capture,
                event="$exception",
                distinct_id=self._service,
                properties={
                    **self._base_properties,
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
                if is_reported(record.exc_info[1]):
                    return
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
    """True unless the installed ``posthog`` is known to set ``$exception_level`` itself.

    An unreadable or unparseable version counts as unknown, so the level is set;
    pre-release suffixes (``7.65.0rc1``) are read by their leading integers.
    """
    try:
        from posthog.version import VERSION  # noqa: PLC0415

        match = re.match(r"(\d+)\.(\d+)\.(\d+)", VERSION)
        return match is None or tuple(map(int, match.groups())) < _SDK_SETS_LEVEL_FROM
    except Exception:
        return True


def is_reported(exc: BaseException | None) -> bool:
    """True once the FastAPI handler has reported ``exc`` (see ``_REPORTED_ATTR``)."""
    return getattr(exc, _REPORTED_ATTR, False)


def mark_reported(exc: BaseException) -> None:
    """Mark ``exc`` as reported so ``PosthogErrorsHandler`` skips its ERROR log."""
    try:
        setattr(exc, _REPORTED_ATTR, True)
    except Exception:  # noqa: S110  an exception type refusing attributes is just re-reported
        pass


def get_current_errors_client() -> PosthogErrorsClient | None:
    """Return the already-built singleton without constructing one, or ``None``."""
    return _client


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
    constructor failed (one warning, then the failure is cached and not retried).
    ``service`` is the event ``distinct_id``; ``environment`` is ``production``,
    ``staging`` or ``development``; ``daily_ceiling`` sets the bucket refill rate.
    ``release`` is stamped on every event when given; ``before_send`` is forwarded
    to the SDK. The first call wins: a later call with a different ``service``,
    ``environment`` or ``daily_ceiling`` gets the existing client and one warning.
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
