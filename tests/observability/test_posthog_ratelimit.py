"""Tests for the PostHog capture rate limiter in wxyc_fastapi.observability.posthog."""

import copy
from unittest.mock import MagicMock, patch

from wxyc_fastapi.observability.posthog import (
    _RateLimitedPosthog,
    get_posthog_client,
)

REPORT_DISTINCT_ID = "wxyc-fastapi-ratelimiter"


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


def _forwarded_events(inner: MagicMock) -> list[str]:
    """Event names of every non-report capture forwarded to the inner client."""
    names = []
    for call in inner.capture.call_args_list:
        event = call.kwargs.get("event")
        if event is None and len(call.args) >= 2:
            event = call.args[1]
        if event != "telemetry_rate_limited":
            names.append(event)
    return names


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
        assert _forwarded_events(inner) == ["e0", "e1", "e2"]

        reports = _report_calls(inner)
        assert len(reports) == 1
        report = reports[0]
        assert report["distinct_id"] == REPORT_DISTINCT_ID
        assert report["properties"]["dropped_count"] == 1
        assert report["properties"]["sample_events"] == ["e3"]
        assert report["properties"]["limit_events_per_minute"] == 60
        assert report["properties"]["burst"] == 3
        assert report["properties"]["trigger_distinct_id"] == "svc"

    def test_refill_allows_later_captures(self):
        inner = MagicMock()
        clock = FakeClock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=clock)

        ph.capture(distinct_id="svc", event="ok0")
        ph.capture(distinct_id="svc", event="dropped")
        clock.advance(1.0)  # 60/min = 1 token per second
        ph.capture(distinct_id="svc", event="ok1")

        assert _forwarded_events(inner) == ["ok0", "ok1"]

    def test_admitted_capture_drains_overdue_drop_accumulation(self):
        inner = MagicMock()
        clock = FakeClock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=clock)

        ph.capture(distinct_id="svc", event="e0")  # admitted, tokens now 0
        ph.capture(distinct_id="svc", event="e1")  # dropped -> immediate report #1
        for i in range(2, 5):  # e2-e4 dropped, accumulate silently
            ph.capture(distinct_id="svc", event=f"e{i}")

        clock.advance(61.0)  # refills exactly 1 token (burst cap); interval elapsed
        ph.capture(distinct_id="svc", event="e5")  # ADMITTED -> drains the pending tail

        reports = _report_calls(inner)
        assert len(reports) == 2
        assert reports[0]["properties"]["dropped_count"] == 1
        assert reports[0]["properties"]["sample_events"] == ["e1"]
        assert reports[1]["properties"]["dropped_count"] == 3  # e2, e3, e4
        assert reports[1]["properties"]["sample_events"] == ["e2", "e3", "e4"]
        assert "e5" in _forwarded_events(inner)

    def test_flush_drains_pending_drop_report(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        ph.capture(distinct_id="svc", event="ok")
        for i in range(4):  # first drop reports immediately, 3 more accumulate
            ph.capture(distinct_id="svc", event=f"d{i}")
        assert len(_report_calls(inner)) == 1

        ph.flush()

        reports = _report_calls(inner)
        assert len(reports) == 2
        assert reports[1]["properties"]["dropped_count"] == 3
        assert reports[1]["properties"]["sample_events"] == ["d1", "d2", "d3"]
        inner.flush.assert_called_once()

    def test_shutdown_drains_pending_drop_report(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        ph.capture(distinct_id="svc", event="ok")
        ph.capture(distinct_id="svc", event="d0")  # immediate report #1
        ph.capture(distinct_id="svc", event="d1")  # accumulates

        ph.shutdown()

        assert len(_report_calls(inner)) == 2
        inner.shutdown.assert_called_once()

    def test_exempt_events_bypass_the_bucket(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(
            inner,
            events_per_minute=60,
            burst=1,
            clock=FakeClock(),
            exempt_events=frozenset({"critical_counter"}),
        )

        ph.capture(distinct_id="svc", event="ok")  # consumes the only token
        for _ in range(3):
            ph.capture(distinct_id="svc", event="critical_counter")
        ph.capture(distinct_id="svc", event="regular")  # dropped

        forwarded = _forwarded_events(inner)
        assert forwarded.count("critical_counter") == 3
        assert "regular" not in forwarded

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
        inner.feature_enabled.assert_called_once_with("some-flag", "uid")

    def test_setattr_forwards_to_inner(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        ph.disabled = True  # the SDK's documented kill switch

        assert inner.disabled is True
        assert "disabled" not in ph.__dict__  # no split-brain shadow on the proxy
        assert ph.disabled is True  # read delegates to the same place the write landed

    def test_shallow_copy_does_not_recurse(self):
        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        clone = copy.copy(ph)  # must not hit RecursionError via __getattr__("_inner")

        assert clone.events_per_minute == 60

    def test_raising_event_str_is_contained(self):
        class Unprintable:
            def __str__(self) -> str:
                raise ValueError("no str for you")

        inner = MagicMock()
        ph = _RateLimitedPosthog(inner, events_per_minute=60, burst=1, clock=FakeClock())

        ph.capture(distinct_id="svc", event="ok")  # exhaust the bucket
        ph.capture(distinct_id="svc", event=Unprintable())  # dropped; must not raise

        reports = _report_calls(inner)
        assert len(reports) == 1
        assert reports[0]["properties"]["sample_events"] == ["<unrepresentable>"]

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
        monkeypatch.delenv("POSTHOG_RATELIMIT_EXEMPT_EVENTS", raising=False)

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

    def test_nonfinite_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.setenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", "nan")
        monkeypatch.setenv("POSTHOG_RATELIMIT_BURST", "inf")

        with patch("posthog.Posthog"):
            client = get_posthog_client("request")

        # nan must not silently disable the limiter, and inf must not pin the
        # bucket full; both fall back to the finite defaults.
        assert isinstance(client, _RateLimitedPosthog)
        assert client.events_per_minute == 60
        assert client.burst == 1800

    def test_nan_burst_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", raising=False)
        monkeypatch.setenv("POSTHOG_RATELIMIT_BURST", "nan")

        with patch("posthog.Posthog"):
            client = get_posthog_client("request")

        # burst=nan would make every admission comparison False and drop 100%
        # of captures forever.
        assert isinstance(client, _RateLimitedPosthog)
        assert client.burst == 1800

    def test_sub_one_burst_clamps_to_one(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", raising=False)
        monkeypatch.setenv("POSTHOG_RATELIMIT_BURST", "0.5")

        with patch("posthog.Posthog"):
            client = get_posthog_client("request")

        # A burst in (0, 1) would permanently drop every capture: tokens are
        # clamped to min(burst, ...) < 1.0 while admission requires >= 1.0.
        assert isinstance(client, _RateLimitedPosthog)
        assert client.burst == 1.0

    def test_env_exempt_events_bypass_bucket(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", raising=False)
        monkeypatch.delenv("POSTHOG_RATELIMIT_BURST", raising=False)
        monkeypatch.setenv("POSTHOG_RATELIMIT_EXEMPT_EVENTS", "critical_a, critical_b")

        with patch("posthog.Posthog"):
            client = get_posthog_client("request")

        assert isinstance(client, _RateLimitedPosthog)
        assert client.exempt_events == frozenset({"critical_a", "critical_b"})

    def test_flush_and_shutdown_reach_inner_client_through_proxy(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_RATELIMIT_EVENTS_PER_MINUTE", raising=False)

        import wxyc_fastapi.observability.posthog as ph_module

        with patch("posthog.Posthog") as MockPosthog:
            instance = MagicMock(name="ph_instance")
            MockPosthog.return_value = instance
            get_posthog_client("request")
            ph_module.flush_posthog()
            ph_module.shutdown_posthog()

        instance.flush.assert_called_once()
        instance.shutdown.assert_called_once()
