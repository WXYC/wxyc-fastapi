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

No ``posthog`` install is needed — these are plain objects satisfying the
client surface that ``RequestTelemetry.send_to_posthog`` and
``get_posthog_client`` consumers use.

Example::

    from wxyc_fastapi.testing import capture_budget

    def test_lookup_emits_one_summary_event():
        with capture_budget(1) as posthog_client:
            run_lookup(..., posthog_client=posthog_client)
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from wxyc_fastapi.observability.posthog import _capture_event_name

if TYPE_CHECKING:
    from collections.abc import Iterator


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

    def capture(self, *args: Any, **kwargs: Any) -> None:
        distinct_id = kwargs.get("distinct_id")
        if distinct_id is None and len(args) >= 2:
            distinct_id = args[0]
        self.captures.append(
            CapturedEvent(
                event=_capture_event_name(args, kwargs),
                distinct_id=distinct_id,
                properties=kwargs.get("properties"),
            )
        )

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
