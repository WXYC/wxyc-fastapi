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

``add_breadcrumb`` and ``capture_exception`` are shims over the PostHog errors client
(``error_tracking.py``), not Sentry; no-ops until ``init_error_tracking`` has run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import sentry_sdk
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

from wxyc_fastapi.observability.errors import get_current_errors_client

if TYPE_CHECKING:
    from sentry_sdk.types import Event, Hint

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
    before_send: Callable[[Event, Hint], Event | None] | None = None,
    before_send_transaction: Callable[[Event, Hint], Event | None] | None = None,
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
        before_send: Optional event filter. Receives a ``sentry_sdk.types.Event``
            and ``Hint``; return ``None`` to drop an event or return the
            (possibly modified) Event to forward it. Useful for filtering
            handled-error classes that you log but don't want tracked in Sentry.
        before_send_transaction: Optional transaction filter, fired once per
            transaction envelope. This is the only hook that can drop
            individual child spans: mutate ``event["spans"]`` (filter out the
            noisy ones) and return the modified Event to keep the transaction
            with fewer spans. Returning ``None`` drops the *entire*
            transaction, including all of its spans — a much blunter
            operation than filtering ``event["spans"]``, and distinct from
            ``before_send``, which only ever sees error events, never
            transactions.
        traces_sample_rate: Performance-tracing sample rate (0.0-1.0). Default
            1.0. This default is deliberately unchanged by the
            ``before_send_transaction`` passthrough above — LML's
            ``lookup/router.py`` measurement series assumes full-fidelity
            traces, so lowering it needs its own PR with a CHANGELOG
            breaking-change note and a consumer fan-out, not a silent bundle
            with a new filtering hook.
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
        before_send_transaction=before_send_transaction,
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
    """Record ``message`` as a ``breadcrumb.<category>`` tag on the current PostHog context.

    The category kwarg is required so each consumer keeps its own taxonomy
    (``"discogs"``, ``"groq"``, ...). The tag rides on events captured inside an
    active ``posthog.contexts.new_context()``; otherwise the breadcrumb is dropped.
    ``data`` and ``level`` are accepted but not recorded.
    """
    try:
        from posthog import contexts  # noqa: PLC0415  lazy: optional ``posthog`` extra
    except ImportError:
        return
    contexts.tag(f"breadcrumb.{category}", message)


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
