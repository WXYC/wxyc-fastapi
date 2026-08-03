"""Tests for wxyc_fastapi.observability.telemetry."""

import inspect
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

    Mirrors the grammar ``as_server_timing`` emits: ``name;dur=<float>`` or
    ``name;desc="...";dur=<float>`` entries joined by ``", "``. Splitting each
    entry's params on a bare ``;`` is safe here because it is only ever fed
    output from ``as_server_timing`` itself, whose ``_sanitize_desc`` strips
    ``;`` from any ``desc`` text before embedding it -- so a semicolon is
    never anything but a param separator in this fixture's input. Raises if
    any ``dur`` is not a float, so it doubles as a round-trip guard for the
    ROM-side parser.
    """
    out: dict[str, float] = {}
    for entry in header.split(", "):
        name, _, params = entry.partition(";")
        dur_param = next((p for p in params.split(";") if p.startswith("dur=")), None)
        assert dur_param is not None, f"bad entry: {entry!r}"
        out[name] = float(dur_param[len("dur=") :])
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


class TestAsServerTimingDescriptions:
    """Per-leg `desc` support (issue #31): `descriptions={name: text}` opts a
    leg into `name;desc="...";dur=<ms>` — used to annotate derived/overlapping
    legs (e.g. LML's discogs = pg_time_ms + api_time_ms) as not additive.
    """

    def test_backward_compatible_without_descriptions(self):
        # Existing callers passing only `extra` (or nothing) are unaffected --
        # `descriptions` is strictly opt-in.
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        header = t.as_server_timing(extra={"discogs": 404.0})
        assert "desc=" not in header
        names = [entry.split(";")[0] for entry in header.split(", ")]
        assert names == ["parse", "discogs", "total"]

    def test_desc_emitted_on_extra_leg_before_dur(self):
        t = make_telemetry()
        header = t.as_server_timing(
            extra={"discogs": 404.0},
            descriptions={"discogs": "overlaps album_lookup, not additive"},
        )
        assert 'discogs;desc="overlaps album_lookup not additive";dur=404' in header

    def test_desc_emitted_on_tracked_step(self):
        t = make_telemetry()
        with t.track_step("metadata_enrichment"):
            pass
        header = t.as_server_timing(descriptions={"metadata_enrichment": "cold cache"})
        assert 'metadata_enrichment;desc="cold cache";dur=' in header

    def test_unknown_description_key_is_ignored(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        header = t.as_server_timing(descriptions={"nonexistent_leg": "whatever"})
        assert "nonexistent_leg" not in header
        assert "desc=" not in header

    def test_leg_without_description_has_no_desc_param(self):
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        with t.track_step("search"):
            pass
        header = t.as_server_timing(descriptions={"search": "annotated"})
        parse_entry = next(e for e in header.split(", ") if e.startswith("parse;"))
        assert "desc=" not in parse_entry

    @pytest.mark.parametrize(
        ("raw", "sanitized"),
        [
            ('has "quotes"', 'has \\"quotes\\"'),
            ("has;semicolon", "hassemicolon"),
            ("has,comma", "hascomma"),
            ("line1\r\nline2", "line1line2"),
            ("back\\slash", "back\\\\slash"),
            # `=` is dropped as defense-in-depth against a `dur=`-scanning parser.
            ("key=value", "keyvalue"),
            # Non-ASCII is dropped (printable-ASCII whitelist): accented Latin-1,
            # CJK, and emoji would otherwise slip past a C0/DEL blacklist and
            # (for anything > U+00FF) raise on the consumer's latin-1 encode.
            ("café", "caf"),
            ("cjk米end", "cjkend"),
            ("note\U0001f3b5here", "notehere"),
            # C1 controls (0x80-0x9f, incl. NEL 0x85) and the Unicode line
            # separators (U+2028/U+2029) are dropped -- a blacklist stopping at
            # C0/DEL would have let these through.
            ("nel\x85csi\x9b", "nelcsi"),
            ("ls ps ", "lsps"),
            ("nul\x00del\x7f", "nuldel"),
        ],
    )
    def test_sanitize_desc_per_character_class(self, raw, sanitized):
        from wxyc_fastapi.observability.telemetry import _sanitize_desc

        assert _sanitize_desc(raw) == sanitized

    def test_malicious_desc_sanitized_header_stays_single_total_and_parseable(self):
        """Centerpiece test: a desc containing `"`, `;`, `,`, and a newline must
        not corrupt the header -- a strict, quoting-unaware parser (like
        request-o-matic's parse_server_timing, which splits on raw `,` / `;`)
        must still see exactly one `total` leg and recover the correct `dur`
        values for every leg.
        """
        t = make_telemetry()
        with t.track_step("parse"):
            pass
        malicious = 'ends with quote " next; comma, then\r\nnewline'
        header = t.as_server_timing(
            extra={"discogs": 404.0},
            descriptions={"discogs": malicious},
        )

        # Structural safety: a naive comma-split (what an unquoting-unaware
        # strict parser does) must not produce more top-level segments than
        # there are legs -- no comma smuggled through from the malicious desc.
        segments = [s for s in header.split(",") if s.strip()]
        assert len(segments) == 3  # parse, discogs(+desc), total

        # Exactly one total, and it is a bare (undescribed) leg.
        assert header.count("total;dur=") == 1
        assert ";total;" not in f";{header}"

        # No raw CR/LF ever reaches the header value.
        assert "\r" not in header
        assert "\n" not in header

        # The whole header survives the consumer's latin-1 header encode
        # (Starlette's MutableHeaders.__setitem__) -- no UnicodeEncodeError.
        header.encode("latin-1")

        # Every dur= is still a parseable float, values are preserved.
        values = _dur_values(header)
        # _fmt_dur rounds to 2 decimals, so an abs tolerance (not the default
        # relative one) is needed for a near-zero live step duration.
        assert values["parse"] == pytest.approx(t.steps["parse"].duration_ms, abs=0.01)
        assert values["discogs"] == 404.0
        assert values["total"] == pytest.approx(t.get_total_duration_ms(), abs=50)
        assert set(values) == {"parse", "discogs", "total"}

        # The desc param carries the sanitized text: structural chars (`;`,
        # `,`, CR, LF) stripped, the literal `"` escaped so the quoted-string
        # stays well-formed.
        assert 'desc="ends with quote \\" next comma thennewline"' in header

    def test_non_ascii_desc_is_dropped_and_header_stays_latin1_encodable(self):
        """A `desc` carrying non-ASCII (accented Latin-1, CJK, emoji, the
        U+2028/U+2029 line separators) or C1 controls must be coerced to
        printable ASCII, so the whole header survives the consumer's
        `.encode("latin-1")` header-set instead of 500-ing on
        UnicodeEncodeError. Regression guard for the C0/DEL-only blacklist that
        let every code point > U+00FF through to the encode boundary.
        """
        t = make_telemetry()
        header = t.as_server_timing(
            extra={"discogs": 404.0},
            descriptions={"discogs": "é\U0001f3b5 sep   \x85\x9b cjk米"},
        )
        # The whole header round-trips through latin-1 (what Starlette does)
        # without raising, and nothing non-ASCII survived into it.
        header.encode("latin-1")
        assert header.isascii()
        # The surviving ASCII text still rides in a well-formed desc leg.
        assert 'discogs;desc=" sep  cjk";dur=404' in header
        values = _dur_values(header)
        assert values["discogs"] == 404.0
        assert set(values) == {"discogs", "total"}

    def test_desc_that_sanitizes_to_empty_falls_back_to_plain_leg(self):
        """A desc made up entirely of stripped bytes (structural delimiters,
        control chars, non-ASCII) must not emit an empty `desc=""` -- the leg
        falls back to the plain `name;dur=` form.
        """
        t = make_telemetry()
        header = t.as_server_timing(
            extra={"discogs": 404.0},
            descriptions={"discogs": ";;,\r\n\U0001f3b5米"},
        )
        assert "desc=" not in header
        assert "discogs;dur=404" in header


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


class TestEmitStepEvents:
    """The ``emit_step_events`` toggle (default ``True``) lets a high-volume
    consumer suppress the per-step PostHog events and emit only the summary,
    without losing any timing data (which the summary already carries via
    ``steps``). Motivated by LML's per-lookup fan-out: 9 tracked steps => ~10
    PostHog events per lookup, none of the per-step events consumed by any
    insight or alert, so a backfill campaign multiplied ingestion ~7x for no
    observability value.
    """

    def test_emit_step_events_true_by_default(self):
        # Default preserves the existing fan-out for consumers that build
        # dashboards on the per-step events (request-o-matic) and for
        # streaming/artist_resolve, which keep the default with no code change.
        assert make_telemetry().emit_step_events is True

    def test_false_suppresses_per_step_events_keeping_only_summary(self):
        client = MagicMock()
        init_cache_stats()
        t = make_telemetry(
            api_call_keys=["discogs"],
            distinct_id="library-metadata-lookup-service",
            event_prefix="lookup",
            emit_step_events=False,
        )
        with t.track_step("library_search"):
            pass
        with t.track_step("album_lookup"):
            pass

        t.send_to_posthog(client)

        # Two tracked steps would normally emit 3 events (2 per-step + summary);
        # with the toggle off, only the summary fires.
        assert client.capture.call_count == 1
        assert client.capture.call_args.kwargs["event"] == "lookup_completed"

    def test_false_still_carries_every_step_timing_in_summary(self):
        # Suppressing per-step events must not lose data: the summary already
        # carries each step's duration under `steps`, so nothing is dropped.
        client = MagicMock()
        init_cache_stats()
        t = make_telemetry(event_prefix="lookup", emit_step_events=False)
        with t.track_step("library_search"):
            pass
        with t.track_step("album_lookup"):
            pass

        t.send_to_posthog(client)

        summary = client.capture.call_args.kwargs["properties"]
        assert summary["steps"] == {
            "library_search_ms": pytest.approx(t.steps["library_search"].duration_ms),
            "album_lookup_ms": pytest.approx(t.steps["album_lookup"].duration_ms),
        }

    def test_true_emits_per_step_events_as_before(self):
        # Explicit True matches the historical behavior exactly (regression
        # guard so the toggle can't silently invert).
        client = MagicMock()
        init_cache_stats()
        t = make_telemetry(event_prefix="lookup", emit_step_events=True)
        with t.track_step("library_search"):
            pass

        t.send_to_posthog(client)

        events = [call.kwargs["event"] for call in client.capture.call_args_list]
        assert events == ["lookup_library_search", "lookup_completed"]

    def test_emit_step_events_is_keyword_only(self):
        # The flag is keyword-only, so inserting it does not shift the positional
        # argument order: the 4th positional field stays `steps`, keeping
        # positional construction backward-compatible with pre-1.3.0 callers.
        kind = inspect.signature(RequestTelemetry).parameters["emit_step_events"].kind
        assert kind is inspect.Parameter.KEYWORD_ONLY

        seeded = {"parse": StepResult(duration_ms=1.0)}
        t = RequestTelemetry(["discogs"], "test-service", "lookup", seeded)
        assert t.steps == seeded
        assert t.emit_step_events is True

    def test_false_with_failing_step_drops_success_error_type_from_summary(self):
        # Pins the documented limitation: suppressing per-step events keeps each
        # step's *timing* in the summary but not its success/error_type — those
        # lived only on the per-step event. Guards the "no timing data lost"
        # claim from being over-read as "no data lost", and stops a silent
        # regression that starts leaking per-step events on the failure path.
        client = MagicMock()
        init_cache_stats()
        t = make_telemetry(event_prefix="lookup", emit_step_events=False)
        with pytest.raises(ValueError, match="boom"):
            with t.track_step("album_lookup"):
                raise ValueError("boom")

        t.send_to_posthog(client)

        # The failing per-step event is suppressed; only the summary fires.
        assert client.capture.call_count == 1
        summary = client.capture.call_args.kwargs["properties"]
        # Timing survives...
        assert summary["steps"] == {
            "album_lookup_ms": pytest.approx(t.steps["album_lookup"].duration_ms)
        }
        # ...but success/error_type are represented nowhere in the summary.
        assert "success" not in summary
        assert "error_type" not in summary
