"""``init_error_tracking``: the one call that replaces ``init_sentry``'s error path.

It builds the ``PosthogErrorsClient`` (``errors.py``), attaches its logging handler
to the root logger, chains ``sys.excepthook`` / ``threading.excepthook`` for
exception autocapture, and registers a FastAPI handler for unhandled route errors.
All three paths send through the one client, so its dedupe window and token bucket
cover them (the SDK's own autocapture stays off: it would bypass that gate). Every path
marks an exception once its capture is admitted and skips marked ones, so one failure
is one event in any order. With ``app``, ``RequestIdMiddleware`` stamps ``request_id``.

``init_sentry`` is untouched, so a consumer can run both during a parity window.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import uuid
from typing import TYPE_CHECKING, Any

from fastapi.responses import PlainTextResponse
from starlette.datastructures import Headers, MutableHeaders

from wxyc_fastapi.observability.errors import (
    PosthogErrorsClient,
    PosthogErrorsHandler,
    get_posthog_errors_client,
    request_id_var,
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
_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_SCOPE_KEY = "wxyc_request_id"
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")


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
    ``development`` (``prod``, ``stage``, ``dev``, ``local`` are aliases); an unknown
    value passes through with one warning. ``daily_ceiling`` is the per-process
    budget in events per day, with no default. ``release`` is stamped on every event when given; ``before_send``
    may return ``None`` to drop an event. Pass ``app`` to install the request-id
    middleware and the unhandled-exception handler. Safe to call more than once: the
    first call's ``service``, ``environment`` and ``daily_ceiling`` win, with one warning
    (a call after ``shutdown_posthog_errors`` leaves the handler and hooks on the old client).
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
        app.add_middleware(RequestIdMiddleware)
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


class RequestIdMiddleware:
    """Keep a valid ``X-Request-ID`` (1-128 of ``[A-Za-z0-9._-]``) or make a uuid4 hex id.

    Echoed on the response, held in ``request_id_var`` and in the ASGI scope (the
    exception handler runs outside user middleware)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        candidate = Headers(scope=scope).get(_REQUEST_ID_HEADER, "")
        request_id = scope.get(_REQUEST_ID_SCOPE_KEY) or (
            candidate if _VALID_REQUEST_ID.fullmatch(candidate) else uuid.uuid4().hex
        )
        scope[_REQUEST_ID_SCOPE_KEY] = request_id
        token = request_id_var.set(request_id)

        async def send_with_id(message: Any) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[_REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


def _unhandled_exception_handler(client: PosthogErrorsClient):
    async def handler(request: Request, exc: Exception) -> PlainTextResponse:
        request_id = request.scope.get(_REQUEST_ID_SCOPE_KEY)
        client.capture_exception(
            (type(exc), exc, exc.__traceback__),
            logger_name="fastapi",
            template="unhandled route exception",
            properties={"request_id": request_id} if request_id else None,
            mechanism={"type": "starlette", "handled": False},
            dedupe_on_message=True,
        )
        return PlainTextResponse("Internal Server Error", status_code=500)

    return handler


def _install_excepthooks(client: PosthogErrorsClient) -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    previous, previous_thread = sys.excepthook, threading.excepthook

    def report(exc_info: Any, template: str, mechanism_type: str) -> None:
        client.capture_exception(
            exc_info,
            logger_name=__name__,
            template=template,
            mechanism={"type": mechanism_type, "handled": False},
            dedupe_on_message=True,
        )

    def excepthook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, Exception):
            report((exc_type, exc, tb), "uncaught exception", "excepthook")
        previous(exc_type, exc, tb)

    def thread_excepthook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit:
            report(
                (args.exc_type, args.exc_value, args.exc_traceback),
                "uncaught thread exception",
                "threading.excepthook",
            )
        previous_thread(args)

    sys.excepthook, threading.excepthook = excepthook, thread_excepthook
