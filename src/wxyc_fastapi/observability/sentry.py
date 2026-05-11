"""Sentry initialization helpers for WXYC FastAPI services.

``init_sentry`` is a thin wrapper around ``sentry_sdk.init`` with three opinions
baked in:

1. ``HttpxIntegration`` is default-on so outbound httpx calls are traced and
   join distributed traces with downstream services. Consumers can override the
   integration list to opt out.
2. A ``service.name`` tag is set globally so events from co-located services
   stay distinguishable in the Sentry UI.
3. The default ``environment`` is ``"local"`` (not ``"production"``) so a stray
   init without arguments does not pollute the production stream.

``add_breadcrumb`` is a category-keyed wrapper around ``sentry_sdk.add_breadcrumb``;
``capture_exception`` optionally attaches a named context dict before sending.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

logger = logging.getLogger(__name__)


def _default_integrations() -> list[Integration]:
    return [FastApiIntegration(), HttpxIntegration()]


def init_sentry(
    dsn: str | None,
    *,
    service_name: str,
    environment: str = "local",
    release: str | None = None,
    integrations: list[Integration] | None = None,
    before_send: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None] | None = None,
    traces_sample_rate: float = 1.0,
    sample_rate: float = 1.0,
) -> None:
    """Initialize the Sentry SDK and set the ``service.name`` tag.

    Args:
        dsn: Sentry DSN. When ``None`` or empty, this is a no-op so callers can
            wire ``init_sentry(os.environ.get("SENTRY_DSN"), ...)`` unconditionally.
        service_name: Logical service name (e.g. ``"library-metadata-lookup"``).
            Set as the global ``service.name`` Sentry tag.
        environment: Sentry ``environment`` tag. Defaults to ``"local"`` so a
            stray call without args does not pollute the production stream.
        release: Optional release version string.
        integrations: Override the default integration list. When ``None``,
            ``FastApiIntegration`` and ``HttpxIntegration`` are enabled.
            ``HttpxIntegration`` being default-on is a deliberate change from
            LML's prior behavior — see CHANGELOG for the consumer-impact note.
        before_send: Optional event filter. Return ``None`` to drop an event;
            return the (possibly modified) event dict to forward it.
        traces_sample_rate: Performance-tracing sample rate (0.0-1.0). Default 1.0.
        sample_rate: Error-event sample rate (0.0-1.0). Default 1.0.
    """
    if not dsn:
        logger.info("Sentry DSN not configured, skipping initialization")
        return

    if integrations is None:
        integrations = _default_integrations()

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        integrations=integrations,
        before_send=before_send,
        traces_sample_rate=traces_sample_rate,
        sample_rate=sample_rate,
    )

    sentry_sdk.set_tag("service.name", service_name)

    logger.info("Sentry initialized (service=%s environment=%s)", service_name, environment)


def add_breadcrumb(
    *,
    category: str,
    message: str,
    data: dict[str, Any] | None = None,
    level: str = "info",
) -> None:
    """Add a Sentry breadcrumb keyed by ``category``.

    The category kwarg is required so each consumer keeps its own taxonomy
    (``"discogs"``, ``"groq"``, ``"slack"``, ...).

    Args:
        category: Breadcrumb category (consumer-defined namespace).
        message: Short description of the operation.
        data: Optional contextual data dict.
        level: Sentry severity (``"debug"``, ``"info"``, ``"warning"``,
            ``"error"``, ``"critical"``).
    """
    sentry_sdk.add_breadcrumb(
        category=category,
        message=message,
        data=data or {},
        level=level,
    )


def capture_exception(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    context_name: str = "request",
) -> None:
    """Capture an exception, optionally attaching a named context dict first.

    Args:
        error: Exception to send.
        context: Optional contextual data attached as a Sentry context.
        context_name: Sentry context key under which ``context`` is stored.
            Defaults to ``"request"``.
    """
    if context:
        sentry_sdk.set_context(context_name, context)
    sentry_sdk.capture_exception(error)
