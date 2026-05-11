"""PostHog client wrapper.

Provides a thread-safe singleton ``Posthog`` client that lazy-initializes from
the ``POSTHOG_API_KEY`` env var. When the key is unset, ``get_posthog_client``
returns ``None`` and warns once per ``event_prefix`` so a misconfigured deploy
surfaces in logs without flooding them.

The ``posthog`` SDK is an optional dependency (the ``[posthog]`` extra). The
top-level module import is free of ``posthog`` references so consumers like
semantic-index — which never call ``get_posthog_client`` — can import
``wxyc_fastapi.observability`` without installing the extra.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "https://us.i.posthog.com"

_client: Posthog | None = None
_warned_prefixes: set[str] = set()
_lock = threading.Lock()


def get_posthog_client(event_prefix: str) -> Posthog | None:
    """Return the singleton ``Posthog`` client, or ``None`` when unconfigured.

    On first call with a missing/empty ``POSTHOG_API_KEY``, logs a single
    warning that names ``event_prefix`` so the misconfigured caller is visible.
    Subsequent calls with the same ``event_prefix`` are silent; calls with a new
    ``event_prefix`` warn again so co-located services each get one warning.

    Args:
        event_prefix: Caller's event-name prefix (e.g. ``"request"``,
            ``"lookup"``). Used for diagnostic logging only — the returned
            client is not auto-prefixing events.

    Returns:
        The shared ``Posthog`` instance, or ``None`` if no API key is set.
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
            _client = Posthog(api_key, host=host)
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
