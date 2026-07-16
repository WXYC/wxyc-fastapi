"""Tests for wxyc_fastapi.observability.telemetry."""

from unittest.mock import MagicMock

import pytest

from wxyc_fastapi.observability.cache_stats import init_cache_stats
from wxyc_fastapi.observability.telemetry import RequestTelemetry, StepResult


def make_telemetry(**overrides) -> RequestTelemetry:
    defaults = {
        "api_call_keys": ["discogs"],
        "distinct_id": "test-service",
        "event_prefix": "lookup",
    }
    defaults.update(overrides)
    return RequestTelemetry(**defaults)


class TestStepResult:
    def test_default_success(self):
        r = StepResult(duration_ms=12.5)
        assert r.duration_ms == 12.5
        assert r.success is True
        assert r.error_type is None

    def test_failure_carries_error_type(self):
        r = StepResult(duration_ms=3.0, success=False, error_type="ValueError")
        assert r.success is False
        assert r.error_type == "ValueError"


class TestApiCallKeysParameterization:
    def test_lml_keys(self):
        t = make_telemetry(api_call_keys=["discogs"])
        assert t.api_calls == {"discogs": 0}

    def test_rom_keys(self):
        t = make_telemetry(api_call_keys=["groq", "discogs", "slack"])
        assert t.api_calls == {"groq": 0, "discogs": 0, "slack": 0}

    def test_record_api_call_increments(self):
        t = make_telemetry(api_call_keys=["groq", "discogs"])
        t.record_api_call("discogs")
        t.record_api_call("discogs")
        t.record_api_call("groq")
        assert t.api_calls == {"groq": 1, "discogs": 2}

    def test_record_unknown_service_is_silent(self, caplog):
        # Following the existing rom/LML behavior: log a warning, do not raise.
        t = make_telemetry(api_call_keys=["discogs"])
        t.record_api_call("apple-music")
        assert t.api_calls == {"discogs": 0}
        assert any("apple-music" in r.message for r in caplog.records)


class TestTrackStep:
    def test_happy_path(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        assert "parse" in t.steps
        result = t.steps["parse"]
        assert result.success is True
        assert result.error_type is None
        assert result.duration_ms >= 0

    def test_exception_records_error_type_and_reraises(self):
        t = make_telemetry()
        with pytest.raises(ValueError, match="boom"):
            with t.track_step("parse"):
                raise ValueError("boom")
        result = t.steps["parse"]
        assert result.success is False
        assert result.error_type == "ValueError"

    def test_step_timings_format(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        with t.track_step("search"):
            pass
        timings = t.get_step_timings()
        assert set(timings.keys()) == {"parse_ms", "search_ms"}

    def test_total_duration_monotonic(self):
        t = make_telemetry()
        d1 = t.get_total_duration_ms()
        d2 = t.get_total_duration_ms()
        assert d2 >= d1


def _dur_values(header: str) -> dict[str, float]:
    """Parse a Server-Timing header value into ``{name: dur}`` for assertions.

    Mirrors the grammar ``as_server_timing`` emits: ``name;dur=<float>`` entries
    joined by ``", "``. Raises if any ``dur`` is not a float, so it doubles as a
    round-trip guard for the ROM-side parser.
    """
    out: dict[str, float] = {}
    for entry in header.split(", "):
        name, _, param = entry.partition(";")
        assert param.startswith("dur="), f"bad entry: {entry!r}"
        out[name] = float(param[len("dur=") :])
    return out


class TestAsServerTiming:
    def test_empty_steps_is_total_only(self):
        t = make_telemetry()
        header = t.as_server_timing()
        # Exactly one entry, and it is the live total. No leading/trailing comma.
        assert header.count(";dur=") == 1
        assert header.startswith("total;dur=")
        assert not header.startswith(", ")
        assert not header.endswith(", ")
        values = _dur_values(header)
        assert set(values) == {"total"}
        assert values["total"] >= 0

    def test_steps_emitted_in_insertion_order_then_total_last(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        with t.track_step("library_search"):
            pass
        with t.track_step("metadata_enrichment"):
            pass
        header = t.as_server_timing()
        names = [entry.split(";")[0] for entry in header.split(", ")]
        assert names == ["parse", "library_search", "metadata_enrichment", "total"]

    def test_extra_appended_after_steps_before_total(self):
        t = make_telemetry()
        with t.track_step("library_search"):
            pass
        with t.track_step("metadata_enrichment"):
            pass
        header = t.as_server_timing(extra={"discogs": 404.0})
        names = [entry.split(";")[0] for entry in header.split(", ")]
        assert names == ["library_search", "metadata_enrichment", "discogs", "total"]

    def test_exactly_one_total_with_steps_and_extra(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        header = t.as_server_timing(extra={"discogs": 12.0, "lookup_service": 40.0})
        assert header.count("total;dur=") == 1

    def test_float_formatting_two_decimals_trailing_zeros_stripped(self):
        t = make_telemetry()
        # Set steps directly so durations are deterministic (track_step is live).
        t.steps["metadata_enrichment"] = StepResult(duration_ms=8102.0)
        t.steps["library_search"] = StepResult(duration_ms=18.4567)
        header = t.as_server_timing(extra={"discogs": 404.00})
        assert "metadata_enrichment;dur=8102" in header  # 8102.00 -> 8102
        assert "library_search;dur=18.46" in header  # rounded to 2 decimals
        assert "discogs;dur=404" in header  # 404.00 -> 404
        # No 3+-decimal residue anywhere.
        assert "18.4567" not in header

    def test_every_dur_is_a_parseable_float(self):
        t = make_telemetry()
        t.steps["parse"] = StepResult(duration_ms=0.0)
        with t.track_step("library_search"):
            pass
        values = _dur_values(t.as_server_timing(extra={"discogs": 404.0}))
        assert values["parse"] == 0.0
        assert values["discogs"] == 404.0
        assert set(values) == {"parse", "library_search", "discogs", "total"}

    def test_no_extra_omits_derived_entries(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        header = t.as_server_timing()
        assert "discogs" not in header
        names = [entry.split(";")[0] for entry in header.split(", ")]
        assert names == ["parse", "total"]


class TestSendToPosthog:
    def test_emits_per_step_and_summary_events(self):
        client = MagicMock()
        init_cache_stats()
        t = make_telemetry(
            api_call_keys=["discogs"],
            distinct_id="library-metadata-lookup-service",
            event_prefix="lookup",
        )
        with t.track_step("parse"):
            pass
        with t.track_step("search"):
            pass

        t.send_to_posthog(client)

        # Two per-step events + 1 summary
        assert client.capture.call_count == 3

        events = [call.kwargs["event"] for call in client.capture.call_args_list]
        assert events == ["lookup_parse", "lookup_search", "lookup_completed"]

        for call in client.capture.call_args_list:
            assert call.kwargs["distinct_id"] == "library-metadata-lookup-service"

        summary = client.capture.call_args_list[-1].kwargs["properties"]
        assert "total_duration_ms" in summary
        assert summary["steps"] == {
            "parse_ms": pytest.approx(t.steps["parse"].duration_ms),
            "search_ms": pytest.approx(t.steps["search"].duration_ms),
        }
        assert summary["api_calls"] == {"discogs": 0}
        assert summary["cache"]["memory_hits"] == 0
        assert summary["cache"]["pg_hits"] == 0

    def test_uses_event_prefix_for_summary_event(self):
        client = MagicMock()
        t = make_telemetry(event_prefix="request")
        t.send_to_posthog(client)
        # No steps, so only the summary event fires.
        assert client.capture.call_count == 1
        assert client.capture.call_args.kwargs["event"] == "request_completed"

    def test_extra_properties_merged_into_summary(self):
        client = MagicMock()
        t = make_telemetry()
        t.send_to_posthog(client, extra_properties={"degraded_mode": "search_unavailable"})
        summary = client.capture.call_args.kwargs["properties"]
        assert summary["degraded_mode"] == "search_unavailable"

    def test_cache_stats_default_when_uninitialized(self):
        # When init_cache_stats was not called for this context, the summary
        # carries a zeroed cache dict rather than None.
        import asyncio

        async def run():
            client = MagicMock()
            t = make_telemetry()
            t.send_to_posthog(client)
            return client.capture.call_args.kwargs["properties"]["cache"]

        cache = asyncio.run(run())
        assert cache == {
            "memory_hits": 0,
            "pg_hits": 0,
            "pg_misses": 0,
            "api_calls": 0,
            "pg_time_ms": 0.0,
            "api_time_ms": 0.0,
        }
