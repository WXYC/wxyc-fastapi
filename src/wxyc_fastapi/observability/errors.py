"""PostHog error reporting: an errors-only client and an ERROR-to-exception logging handler.

``get_posthog_errors_client`` is keyed by ``POSTHOG_ERRORS_API_KEY``, separate
from the analytics ``POSTHOG_API_KEY`` so a service's errors can land in a
different PostHog project than its analytics. With the key unset it returns
``None`` and ``PosthogErrorsHandler`` is never given a client to forward to
(fail-open). Frame locals (``capture_exception_code_variables``) stay off.

``PosthogErrorsHandler`` stands in for Sentry's ``LoggingIntegration``: every
record at ERROR or above is reported. Records carrying ``exc_info`` go through
the SDK's ``capture_exception``; message-only records — which the SDK's
``capture_exception`` silently drops because there is no active exception — are
sent as a synthesized ``$exception`` event whose ``$exception_fingerprint`` is
``[logger name, unformatted msg]``, so occurrences group by call site rather
than by interpolated values.

PostHog's free plan caps exceptions at 100K/month, so the client gates both
paths behind a token bucket (``ERRORS_EVENTS_PER_MINUTE`` sustained,
``ERRORS_BURST`` capacity) and a same-fingerprint dedupe window
(``ERRORS_DEDUPE_WINDOW_SECONDS``). Like the analytics limiter this is a
per-process circuit breaker, not a budget enforcer: N workers admit N× the rate.
"""

from __future__ import annotations

import logging
import os
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

ERRORS_EVENTS_PER_MINUTE = 30.0
ERRORS_BURST = 100.0
ERRORS_DEDUPE_WINDOW_SECONDS = 60.0

_MAX_TRACKED_FINGERPRINTS = 1000
_DISTINCT_ID = "wxyc-fastapi-errors"
# Logger-name prefixes whose output must never be re-reported: the SDK's own
# diagnostics (its consumer thread logs failed sends) and this package's.
_SELF_LOGGER_PREFIXES = ("posthog", "wxyc_fastapi")

_client: PosthogErrorsClient | None = None
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
        events_per_minute: float = ERRORS_EVENTS_PER_MINUTE,
        burst: float = ERRORS_BURST,
        dedupe_window_seconds: float = ERRORS_DEDUPE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._rate_per_second = events_per_minute / 60.0
        self._burst = burst
        self._dedupe_window = dedupe_window_seconds
        self._clock = clock
        self._gate_lock = threading.Lock()
        self._tokens = burst
        self._last_refill = clock()
        self._seen: dict[tuple[str, ...], float] = {}

    def capture_exception(self, exc_info: ExcInfo, *, logger_name: str, template: str) -> None:
        """Report a record's active exception via the SDK's ``capture_exception``."""
        if self._admit((logger_name, template, exc_info[0].__name__)):
            self._inner.capture_exception(exc_info, distinct_id=_DISTINCT_ID)

    def capture_message(self, *, logger_name: str, template: str, message: str) -> None:
        """Report a message-only record as a synthesized ``$exception`` event."""
        if self._admit((logger_name, template)):
            self._inner.capture(
                event="$exception",
                distinct_id=_DISTINCT_ID,
                properties={
                    "$exception_list": [
                        {
                            "type": logger_name,
                            "value": message,
                            "mechanism": {"handled": True, "type": "logging"},
                        }
                    ],
                    "$exception_fingerprint": [logger_name, template],
                },
            )

    def flush(self) -> Any:
        return self._inner.flush()

    def shutdown(self) -> Any:
        return self._inner.shutdown()

    def _admit(self, key: tuple[str, ...]) -> bool:
        with self._gate_lock:
            now = self._clock()
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

    def _evict_locked(self, now: float) -> None:
        """Drop expired entries; if none expired, drop the oldest so the table stays bounded."""
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._dedupe_window}
        if len(self._seen) >= _MAX_TRACKED_FINGERPRINTS:
            del self._seen[min(self._seen, key=self._seen.__getitem__)]


class PosthogErrorsHandler(logging.Handler):
    """Forward ERROR-and-above log records to a ``PosthogErrorsClient``.

    Never raises into the logging call site, and never reports records from the
    PostHog SDK or this package (including any logged re-entrantly while a
    capture is in flight on the same thread).
    """

    def __init__(self, client: PosthogErrorsClient, level: int = logging.ERROR) -> None:
        super().__init__(level)
        self._errors_client = client
        self._emitting = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.split(".")[0] in _SELF_LOGGER_PREFIXES or getattr(
            self._emitting, "active", False
        ):
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


def get_posthog_errors_client() -> PosthogErrorsClient | None:
    """Return the singleton errors client, or ``None`` when ``POSTHOG_ERRORS_API_KEY`` is unset."""
    global _client
    with _lock:
        api_key = os.environ.get("POSTHOG_ERRORS_API_KEY")
        if not api_key:
            return None
        if _client is None:
            from posthog import Posthog  # noqa: PLC0415  lazy: only needed when key is set

            host = os.environ.get("POSTHOG_HOST", _DEFAULT_HOST)
            _client = PosthogErrorsClient(
                Posthog(api_key, host=host, capture_exception_code_variables=False)
            )
        return _client


def shutdown_posthog_errors() -> None:
    """Shut down the errors client and clear the singleton. Safe no-op when uninitialized."""
    global _client
    if _client is not None:
        _client.shutdown()
        _client = None
