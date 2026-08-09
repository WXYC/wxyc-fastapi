"""Tests for the PostHog capture rate limiter in wxyc_fastapi.observability.posthog."""

from unittest.mock import MagicMock, patch

import pytest

import wxyc_fastapi.observability.posthog as ph_module
from wxyc_fastapi.observability.posthog import (
    _RateLimitedPosthog,
    get_posthog_client,
)


@pytest.fixture(autouse=True)
def _reset_posthog_state():
    """Each test starts with a fresh client + empty warn-once set."""
    ph_module._client = None
    ph_module._warned_prefixes.clear()
    yield
    ph_module._client = None
    ph_module._warned_prefixes.clear()


class FakeClock:
    """Deterministic monotonic clock for driving token-bucket refill."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _report_calls(inner: MagicMock) -> list[dict]:
    """kwargs of every telemetry_rate_limited report the proxy emitted."""
    return [
        call.kwargs
        for call in inner.capture.call_args_list
        if call.kwargs.get("event") == "telemetry_rate_limited"
    ]


class TestRateLimitedCapture:
    def test_forwards_within_budget(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=5, clock=FakeClock())

        for i in range(5):
            ph.capture(distinct_id="svc", event=f"e{i}", properties={"i": i})

        assert inner.capture.call_count == 5
        first = inner.capture.call_args_list[0]
        assert first.kwargs["distinct_id"] == "svc"
        assert first.kwargs["event"] == "e0"
        assert first.kwargs["properties"] == {"i": 0}

    def test_drops_beyond_burst_and_reports_once(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=3, clock=FakeClock())

        for i in range(5):
            ph.capture(distinct_id="svc", event=f"e{i}")

        # e0-e2 forwarded; e3 dropped (immediate first report); e4 dropped silently.
        forwarded = [
            c.kwargs["event"]
            for c in inner.capture.call_args_list
            if c.kwargs.get("event") != "telemetry_rate_limited"
        ]
        assert forwarded == ["e0", "e1", "e2"]

        reports = _report_calls(inner)
        assert len(reports) == 1
        report = reports[0]
        assert report["distinct_id"] == "svc"
        assert report["properties"]["dropped_count"] == 1
        assert report["properties"]["sample_events"] == ["e3"]
        assert report["properties"]["limit_events_per_minute"] == 60
        assert report["properties"]["burst"] == 3

    def test_refill_allows_later_captures(self):
        inner = MagicMock()
        clock = FakeClock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=clock)

        ph.capture(distinct_id="svc", event="ok0")
        ph.capture(distinct_id="svc", event="dropped")
        clock.advance(1.0)  # 60/min = 1 token per second
        ph.capture(distinct_id="svc", event="ok1")

        forwarded = [
            c.kwargs["event"]
            for c in inner.capture.call_args_list
            if c.kwargs.get("event") != "telemetry_rate_limited"
        ]
        assert forwarded == ["ok0", "ok1"]

    def test_report_batches_drops_across_interval(self):
        inner = MagicMock()
        clock = FakeClock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=clock)

        ph.capture(distinct_id="svc", event="e0")  # admitted, tokens now 0
        ph.capture(distinct_id="svc", event="e1")  # dropped -> immediate report #1
        for i in range(2, 5):  # e2-e4 dropped, accumulate silently
            ph.capture(distinct_id="svc", event=f"e{i}")

        clock.advance(61.0)  # refills exactly 1 token (burst cap)
        ph.capture(distinct_id="svc", event="e5")  # admitted, tokens back to 0
        ph.capture(distinct_id="svc", event="e6")  # dropped -> report #2 (interval elapsed)

        reports = _report_calls(inner)
        assert len(reports) == 2
        assert reports[0]["properties"]["dropped_count"] == 1
        assert reports[0]["properties"]["sample_events"] == ["e1"]
        assert reports[1]["properties"]["dropped_count"] == 4  # e2, e3, e4, e6
        assert reports[1]["properties"]["sample_events"] == ["e2", "e3", "e4", "e6"]

    def test_positional_capture_form_supported(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        ph.capture("uid", "evt_a")
        ph.capture("uid", "evt_b")

        assert inner.capture.call_args_list[0].args == ("uid", "evt_a")
        reports = _report_calls(inner)
        assert len(reports) == 1
        assert reports[0]["properties"]["sample_events"] == ["evt_b"]

    def test_getattr_delegates_to_inner(self):
        inner = MagicMock()
        inner.feature_enabled.return_value = True
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        assert ph.feature_enabled("some-flag", "uid") is True
        ph.flush()
        inner.feature_enabled.assert_called_once_with("some-flag", "uid")
        inner.flush.assert_called_once()

    def test_report_emit_failure_is_swallowed(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        ph.capture(distinct_id="svc", event="ok")
        inner.capture.side_effect = RuntimeError("posthog down")
        ph.capture(distinct_id="svc", event="dropped")  # report emit raises -> swallowed


class TestGetPosthogClientRateLimiting:
    def test_wraps_by_default(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", raising=False)
        monkeypatch.delenv("POSTHOG_RATELIMIT_BURST", raising=False)

        with patch("posthog.Posthog") as MockPosthog:
            MockPosthog.return_value = MagicMock(name="ph_instance")
            client = get_posthog_client("request")

        assert isinstance(client, _RateLimitedPosthog)
        assert client.events_per_minute == 60
        assert client.burst == 1800

    def test_env_overrides_limits(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.setenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", "120")
        monkeypatch.setenv("POSTHOG_RATELIMIT_BURST", "500")

        with patch("posthog.Posthog"):
            client = get_posthog_client("request")

        assert isinstance(client, _RateLimitedPosthog)
        assert client.events_per_minute == 120
        assert client.burst == 500

    def test_zero_rate_disables_wrapping(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.setenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", "0")

        with patch("posthog.Posthog") as MockPosthog:
            MockPosthog.return_value = MagicMock(name="ph_instance")
            client = get_posthog_client("request")

        assert client is MockPosthog.return_value

    def test_invalid_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.setenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", "banana")
        monkeypatch.setenv("POSTHOG_RATELIMIT_BURST", "-5")

        with patch("posthog.Posthog"):
            client = get_posthog_client("request")

        assert isinstance(client, _RateLimitedPosthog)
        assert client.events_per_minute == 60
        assert client.burst == 1800

    def test_flush_and_shutdown_reach_inner_client_through_proxy(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", raising=False)

        with patch("posthog.Posthog") as MockPosthog:
            instance = MagicMock(name="ph_instance")
            MockPosthog.return_value = instance
            get_posthog_client("request")
            ph_module.flush_posthog()
            ph_module.shutdown_posthog()

        instance.flush.assert_called_once()
        instance.shutdown.assert_called_once()
