"""``init_error_tracking``: the one call that replaces ``init_sentry``'s error path.

It builds the ``PosthogErrorsClient`` (``errors.py``), attaches its logging handler
to the root logger, chains ``sys.excepthook`` / ``threading.excepthook`` for
exception autocapture, and registers a FastAPI handler for unhandled route errors.
All three paths send through the one client, so its dedupe window and token bucket
cover them; the SDK's own ``enable_exception_autocapture`` is left off because it
would bypass that gate.

Starlette re-raises an unhandled exception after the handler runs, and uvicorn then
logs it at ERROR with ``exc_info``. The handler marks the exception it reported and
``PosthogErrorsHandler`` skips marked exceptions, so one failure is one event.

``init_sentry`` is untouched, so a consumer can run both during a parity window.
"""

from __future__ import annotations

import logging
import sys
import threading
from typing import TYPE_CHECKING, Any

from fastapi.responses import PlainTextResponse

from wxyc_fastapi.observability.errors import (
    PosthogErrorsClient,
    PosthogErrorsHandler,
    get_posthog_errors_client,
    is_reported,
    mark_reported,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

# The contract allows exactly production, staging and development; common aliases
# map onto them. Same table as wxyc-etl's ``_normalize_environment``.
_ENVIRONMENT_ALIASES = {
    "production": "production",
    "prod": "production",
    "staging": "staging",
    "stage": "staging",
    "development": "development",
    "dev": "development",
    "local": "development",
}
_warned_environments: set[str] = set()
_hooks_installed = False


def init_error_tracking(
    *,
    service: str,
    environment: str,
    daily_ceiling: float,
    release: str | None = None,
    before_send: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    app: FastAPI | None = None,
) -> PosthogErrorsClient | None:
    """Wire PostHog error tracking for a service; return the client, or ``None`` when disabled.

    ``None`` (``POSTHOG_ERRORS_API_KEY`` unset, or the SDK unavailable) leaves nothing
    installed. ``environment`` is normalized to ``production``, ``staging`` or
    ``development`` (``prod``, ``stage``, ``dev`` and ``local`` are aliases); an
    unknown value passes through with one warning. ``daily_ceiling`` is the
    per-process budget in events per day, with no default because the sizes differ
    per service. ``release`` is stamped on every event when given; ``before_send``
    receives each SDK event and may return ``None`` to drop it. Pass ``app`` to
    register the unhandled-exception handler on it. Safe to call more than once: the
    first call's ``service``, ``environment`` and ``daily_ceiling`` win, with one warning.
    """
    client = get_posthog_errors_client(
        service=service,
        environment=_normalize_environment(environment),
        daily_ceiling=daily_ceiling,
        release=release,
        before_send=before_send,
    )
    if client is None:
        return None
    root = logging.getLogger()
    if not any(isinstance(h, PosthogErrorsHandler) for h in root.handlers):
        root.addHandler(PosthogErrorsHandler(client))
    _install_excepthooks(client)
    if app is not None:
        app.add_exception_handler(Exception, _unhandled_exception_handler(client))
    return client


def _normalize_environment(value: str) -> str:
    mapped = _ENVIRONMENT_ALIASES.get(value.strip().lower())
    if mapped is not None:
        return mapped
    if value not in _warned_environments:
        _warned_environments.add(value)
        logger.warning(
            "environment %r is not one of production, staging or development "
            "(error-tracking contract); passing it through",
            value,
        )
    return value


def _unhandled_exception_handler(client: PosthogErrorsClient):
    async def handler(request: Request, exc: Exception) -> PlainTextResponse:
        _report(client, (type(exc), exc, exc.__traceback__), "unhandled route exception")
        mark_reported(exc)
        return PlainTextResponse("Internal Server Error", status_code=500)

    return handler


def _report(client: PosthogErrorsClient, exc_info: Any, template: str) -> None:
    if exc_info[1] is not None and not is_reported(exc_info[1]):
        client.capture_exception(exc_info, logger_name=__name__, template=template)


def _install_excepthooks(client: PosthogErrorsClient) -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    previous, previous_thread = sys.excepthook, threading.excepthook

    def excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, Exception):
            _report(client, (exc_type, exc, tb), "uncaught exception")
        previous(exc_type, exc, tb)

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit:
            _report(
                client,
                (args.exc_type, args.exc_value, args.exc_traceback),
                "uncaught thread exception",
            )
        previous_thread(args)

    sys.excepthook, threading.excepthook = excepthook, thread_excepthook
