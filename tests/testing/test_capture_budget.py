"""Tests for wxyc_fastapi.testing capture-budget helpers."""

import pytest

from wxyc_fastapi.testing import CountingPosthog, capture_budget


class TestCountingPosthog:
    def test_records_kwargs_capture(self):
        ph = CountingPosthog()
        ph.capture(distinct_id="svc", event="lookup_completed", properties={"a": 1})

        assert len(ph.captures) == 1
        captured = ph.captures[0]
        assert captured.event == "lookup_completed"
        assert captured.distinct_id == "svc"
        assert captured.properties == {"a": 1}

    def test_records_positional_capture(self):
        ph = CountingPosthog()
        ph.capture("uid", "evt_a")

        assert ph.events == ["evt_a"]
        assert ph.captures[0].distinct_id == "uid"

    def test_count_total_and_per_event(self):
        ph = CountingPosthog()
        ph.capture(distinct_id="svc", event="a")
        ph.capture(distinct_id="svc", event="a")
        ph.capture(distinct_id="svc", event="b")

        assert ph.count() == 3
        assert ph.count("a") == 2
        assert ph.count("b") == 1
        assert ph.count("missing") == 0

    def test_flush_and_shutdown_are_noops(self):
        ph = CountingPosthog()
        ph.flush()
        ph.shutdown()


class TestCaptureBudget:
    def test_passes_at_or_under_budget(self):
        with capture_budget(2) as ph:
            ph.capture(distinct_id="svc", event="a")
            ph.capture(distinct_id="svc", event="b")

    def test_raises_over_budget_with_event_names(self):
        with pytest.raises(AssertionError) as excinfo:
            with capture_budget(1) as ph:
                ph.capture(distinct_id="svc", event="lookup_completed")
                ph.capture(distinct_id="svc", event="lookup_album_lookup")

        message = str(excinfo.value)
        assert "2" in message
        assert "1" in message
        assert "lookup_album_lookup" in message

    def test_counts_only_events_inside_block_for_external_client(self):
        ph = CountingPosthog()
        ph.capture(distinct_id="svc", event="before")  # outside the budget window

        with capture_budget(1, client=ph):
            ph.capture(distinct_id="svc", event="inside")

    def test_zero_budget_flags_any_capture(self):
        with pytest.raises(AssertionError):
            with capture_budget(0) as ph:
                ph.capture(distinct_id="svc", event="a")
