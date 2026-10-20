"""Test helpers for PostHog instrumentation budgets.

``CountingPosthog`` is an in-memory stand-in for the ``posthog.Posthog``
client: it records every ``capture(...)`` and no-ops ``flush``/``shutdown``,
so a test can drive real code paths and then assert on exactly what would have
been billed. ``capture_budget`` wraps it in an assertion: a flow that captures
more events than its budget fails the test.

The helpers exist because a PostHog capture is a billing decision: the
2026-08-04 org-wide quota cutoff was caused by an instrumentation path whose
per-operation event count was never pinned by a test. Budget the hot paths:
one operation should emit at most one summary event, and a bulk job over N
items should emit O(1) events per run, not O(N).

Fidelity note: ``capture`` mirrors the posthog 7.x signature the consumers pin
— ``capture(event, *, distinct_id=..., properties=..., ...)``. The legacy
``capture("uid", "event")`` two-positional form raises ``TypeError`` here; the
real 7.x SDK logs an error and silently discards that call, so a budget test
must fail loudly rather than certify telemetry production would drop.

No ``posthog`` install is needed — these are plain objects satisfying the
client surface that ``RequestTelemetry.send_to_posthog`` and
``get_posthog_client`` consumers use. Where a call site is annotated
``Posthog | None``, pass the fake through :func:`as_posthog` to satisfy mypy.

Example::

    from wxyc_fastapi.testing import as_posthog, capture_budget

    def test_lookup_emits_one_summary_event():
        with capture_budget(1) as ph:
            run_lookup(..., posthog_client=as_posthog(ph))
"""

from __future__ import annotations

import logging
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast
from unittest import mock

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

    from posthog import Posthog


@dataclass(frozen=True)
class CapturedEvent:
    """One recorded ``capture(...)`` call."""

    event: str
    distinct_id: str | None
    properties: dict[str, Any] | None


@dataclass
class CountingPosthog:
    """In-memory PostHog client stand-in that records captures."""

    captures: list[CapturedEvent] = field(default_factory=list)

    def capture(
        self,
        event: str,
        *,
        distinct_id: str | None = None,
        properties: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        """Record a capture. Signature mirrors the posthog 7.x SDK, so the
        legacy two-positional form fails here exactly where production would
        silently drop it."""
        self.captures.append(
            CapturedEvent(event=event, distinct_id=distinct_id, properties=properties)
        )

    def capture_exception(
        self,
        exception: BaseException | None = None,
        distinct_id: str | None = None,
        properties: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        """Record an exception capture as a ``$exception`` event."""
        self.captures.append(
            CapturedEvent(event="$exception", distinct_id=distinct_id, properties=properties)
        )

    def feature_enabled(self, *_args: Any, **_kwargs: Any) -> bool | None:
        """Flag reads resolve to ``None`` (unknown flag), recording nothing."""
        return None

    def get_feature_flag(self, *_args: Any, **_kwargs: Any) -> Any:
        """Flag reads resolve to ``None`` (unknown flag), recording nothing."""
        return None

    def flush(self) -> None:
        """No-op, present so code that flushes a real client runs unchanged."""

    def shutdown(self) -> None:
        """No-op, present so code that shuts down a real client runs unchanged."""

    @property
    def events(self) -> list[str]:
        """Captured event names, in capture order."""
        return [captured.event for captured in self.captures]

    def count(self, event: str | None = None) -> int:
        """Number of captures, optionally filtered to one event name."""
        if event is None:
            return len(self.captures)
        return sum(1 for captured in self.captures if captured.event == event)


def as_posthog(client: CountingPosthog) -> Posthog:
    """Type-cast a :class:`CountingPosthog` for ``Posthog``-annotated call sites.

    The fake has no runtime relationship to ``posthog.Posthog`` (importing the
    SDK is exactly what these helpers avoid), so consumer code annotated
    ``Posthog | None`` fails mypy when handed the fake directly. This is a
    zero-cost ``typing.cast`` — it returns the same object.
    """
    return cast("Posthog", client)


@contextmanager
def capture_budget(
    max_events: int, client: CountingPosthog | None = None
) -> Iterator[CountingPosthog]:
    """Assert that the with-block captures at most ``max_events`` events.

    Yields a ``CountingPosthog`` (the one passed as ``client``, or a fresh one)
    to hand to the code under test. Only captures recorded inside the block
    count against the budget, so a shared client can cross multiple budget
    windows. Raises ``AssertionError`` naming the captured events when the
    budget is exceeded. If the block itself raises, that exception propagates
    and the budget is not checked — the real failure stays primary.
    """
    posthog_client = client if client is not None else CountingPosthog()
    start = len(posthog_client.captures)
    yield posthog_client
    captured = posthog_client.captures[start:]
    if len(captured) > max_events:
        names = ", ".join(c.event for c in captured)
        raise AssertionError(
            f"PostHog capture budget exceeded: {len(captured)} events captured, "
            f"budget {max_events}. Events: {names}"
        )


@contextmanager
def isolated_error_tracking(stub: CountingPosthog | None = None) -> Iterator[CountingPosthog]:
    """Run the with-block against a fresh, stubbed error-tracking singleton.

    Sets ``POSTHOG_ERRORS_API_KEY``, clears the errors client singleton, and patches
    ``posthog.Posthog`` to return ``stub`` (a fresh ``CountingPosthog`` by default),
    which is yielded. On exit, even when the block raises, the root logger's handlers,
    ``sys.excepthook``, ``threading.excepthook``, the excepthook-installed flag and the
    singleton are put back, and the env var is restored. Needs the ``posthog`` extra.
    """
    from wxyc_fastapi.observability import error_tracking, errors  # noqa: PLC0415

    posthog_stub = stub if stub is not None else CountingPosthog()
    root = logging.getLogger()
    saved = (
        root.handlers[:],
        sys.excepthook,
        threading.excepthook,
        error_tracking._hooks_installed,
        (errors._client, errors._config, errors._init_failed),
    )
    errors._client, errors._config, errors._init_failed = None, None, False
    error_tracking._hooks_installed = False
    try:
        with (
            mock.patch.dict("os.environ", {"POSTHOG_ERRORS_API_KEY": "phc_test"}),
            mock.patch("posthog.Posthog", return_value=posthog_stub),
        ):
            yield posthog_stub
    finally:
        root.handlers[:] = saved[0]
        sys.excepthook, threading.excepthook = saved[1], saved[2]
        error_tracking._hooks_installed = saved[3]
        errors._client, errors._config, errors._init_failed = saved[4]


@pytest.fixture
def isolated_error_tracking_stub() -> Iterator[CountingPosthog]:
    """Pytest fixture form of :func:`isolated_error_tracking`; yields its stub.

    Import it into a ``conftest.py`` (``from wxyc_fastapi.testing import
    isolated_error_tracking_stub``) to use it by name.
    """
    with isolated_error_tracking() as stub:
        yield stub
