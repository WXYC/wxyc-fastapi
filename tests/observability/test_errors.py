"""Tests for the PostHog errors client and ERROR-to-exception logging handler.

Singleton state reset lives in this directory's conftest.py.
"""

import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

import wxyc_fastapi.observability.errors as errors_module
from wxyc_fastapi.observability.errors import (
    ERRORS_BURST,
    ERRORS_DEDUPE_WINDOW_SECONDS,
    ERRORS_EVENTS_PER_MINUTE,
    PosthogErrorsClient,
    PosthogErrorsHandler,
    get_posthog_errors_client,
    shutdown_posthog_errors,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _record(
    msg: str = "boom",
    args: tuple = (),
    *,
    name: str = "svc.lookup",
    level: int = logging.ERROR,
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, msg, args, exc_info)


def _client(inner: MagicMock, clock: FakeClock | None = None, **kwargs) -> PosthogErrorsClient:
    return PosthogErrorsClient(inner, clock=clock or FakeClock(), **kwargs)


def _exc_info():
    try:
        raise ValueError("kaboom")
    except ValueError:
        return sys.exc_info()


class TestGetPosthogErrorsClient:
    def test_none_when_key_unset(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_ERRORS_API_KEY", raising=False)
        assert get_posthog_errors_client() is None

    def test_none_when_only_analytics_key_set(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_ERRORS_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_analytics")
        assert get_posthog_errors_client() is None

    def test_constructs_with_errors_key_and_code_variables_off(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_analytics")
        monkeypatch.delenv("POSTHOG_HOST", raising=False)
        with patch("posthog.Posthog") as mock_cls:
            client = get_posthog_errors_client()
        assert isinstance(client, PosthogErrorsClient)
        args, kwargs = mock_cls.call_args
        assert args == ("phc_errors",)
        assert kwargs["host"] == "https://us.i.posthog.com"
        assert kwargs["capture_exception_code_variables"] is False

    def test_is_a_singleton_independent_of_analytics_client(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog"):
            assert get_posthog_errors_client() is get_posthog_errors_client()

    def test_shutdown_shuts_inner_down_and_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog") as mock_cls:
            first = get_posthog_errors_client()
            shutdown_posthog_errors()
            mock_cls.return_value.shutdown.assert_called_once()
            assert get_posthog_errors_client() is not first

    def test_shutdown_is_noop_without_client(self):
        shutdown_posthog_errors()


class TestHandlerWithExcInfo:
    def test_routes_to_capture_exception(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))
        exc_info = _exc_info()

        handler.handle(_record(exc_info=exc_info))

        inner.capture_exception.assert_called_once()
        assert inner.capture_exception.call_args.args[0] is exc_info
        inner.capture.assert_not_called()

    def test_capture_exception_failure_is_swallowed(self):
        inner = MagicMock()
        inner.capture_exception.side_effect = RuntimeError("sdk down")
        handler = PosthogErrorsHandler(_client(inner))
        handler.handle(_record(exc_info=_exc_info()))  # must not raise


class TestHandlerWithoutExcInfo:
    def test_synthesizes_exception_event(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))

        handler.handle(_record("lookup failed for %s", ("Juana Molina",)))

        inner.capture_exception.assert_not_called()
        inner.capture.assert_called_once()
        kwargs = inner.capture.call_args.kwargs
        assert kwargs["event"] == "$exception"
        props = kwargs["properties"]
        assert props["$exception_list"] == [
            {
                "type": "svc.lookup",
                "value": "lookup failed for Juana Molina",
                "mechanism": {"handled": True, "type": "logging"},
            }
        ]

    def test_fingerprint_uses_unformatted_template(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))

        handler.handle(_record("lookup failed for %s", ("Juana Molina",)))
        handler.handle(_record("lookup failed for %s", ("Stereolab",)))

        fingerprints = [
            c.kwargs["properties"]["$exception_fingerprint"] for c in inner.capture.call_args_list
        ]
        # The second record is deduped, but both would share this fingerprint.
        assert fingerprints == [["svc.lookup", "lookup failed for %s"]]

    def test_levels_below_error_are_ignored(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))
        logger = logging.getLogger("svc.levels")
        logger.propagate = False
        logger.addHandler(handler)
        try:
            logger.warning("not an error")
            logger.error("an error")
        finally:
            logger.removeHandler(handler)
        assert inner.capture.call_count == 1

    def test_capture_failure_is_swallowed(self):
        inner = MagicMock()
        inner.capture.side_effect = RuntimeError("sdk down")
        PosthogErrorsHandler(_client(inner)).handle(_record())  # must not raise


class TestReentrancy:
    @pytest.mark.parametrize(
        "name", ["posthog", "posthog.client", "wxyc_fastapi.observability.posthog", "wxyc_fastapi"]
    )
    def test_own_logger_output_is_not_captured(self, name):
        inner = MagicMock()
        PosthogErrorsHandler(_client(inner)).handle(_record(name=name))
        inner.capture.assert_not_called()
        inner.capture_exception.assert_not_called()

    def test_logging_error_from_inside_capture_does_not_recurse(self):
        inner = MagicMock()
        logger = logging.getLogger("svc.reentrant")
        logger.propagate = False
        handler = PosthogErrorsHandler(_client(inner))
        logger.addHandler(handler)
        inner.capture.side_effect = lambda **_: logger.error("sdk hiccup")
        try:
            logger.error("first")
        finally:
            logger.removeHandler(handler)
        assert inner.capture.call_count == 1


class TestBucket:
    def test_defaults_are_named_constants(self):
        assert ERRORS_EVENTS_PER_MINUTE > 0
        assert ERRORS_BURST >= 1
        assert ERRORS_DEDUPE_WINDOW_SECONDS > 0

    def test_drops_beyond_burst_across_both_paths(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner, events_per_minute=60, burst=2))

        handler.handle(_record("a"))
        handler.handle(_record("b", exc_info=_exc_info()))
        handler.handle(_record("c"))

        assert inner.capture.call_count + inner.capture_exception.call_count == 2

    def test_refill_admits_later_errors(self):
        inner = MagicMock()
        clock = FakeClock()
        handler = PosthogErrorsHandler(_client(inner, clock, events_per_minute=60, burst=1))

        handler.handle(_record("a"))
        handler.handle(_record("b"))  # dropped
        clock.advance(1.0)
        handler.handle(_record("c"))

        assert inner.capture.call_count == 2

    def test_dropped_records_do_not_consume_tokens_via_dedupe(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner, events_per_minute=60, burst=2))

        handler.handle(_record("same"))
        handler.handle(_record("same"))  # deduped, costs no token
        handler.handle(_record("other"))

        assert inner.capture.call_count == 2


class TestDedupe:
    def test_same_fingerprint_inside_window_is_dropped(self):
        inner = MagicMock()
        clock = FakeClock()
        handler = PosthogErrorsHandler(_client(inner, clock))

        handler.handle(_record("same %s", ("1",)))
        clock.advance(ERRORS_DEDUPE_WINDOW_SECONDS - 1)
        handler.handle(_record("same %s", ("2",)))

        assert inner.capture.call_count == 1

    def test_same_fingerprint_after_window_is_admitted(self):
        inner = MagicMock()
        clock = FakeClock()
        handler = PosthogErrorsHandler(_client(inner, clock))

        handler.handle(_record("same"))
        clock.advance(ERRORS_DEDUPE_WINDOW_SECONDS + 1)
        handler.handle(_record("same"))

        assert inner.capture.call_count == 2

    def test_different_logger_or_template_is_not_deduped(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))

        handler.handle(_record("same", name="svc.a"))
        handler.handle(_record("same", name="svc.b"))
        handler.handle(_record("different", name="svc.a"))

        assert inner.capture.call_count == 3

    def test_exception_type_distinguishes_exc_info_records(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))
        try:
            raise KeyError("k")
        except KeyError:
            key_error = sys.exc_info()

        handler.handle(_record("same", exc_info=_exc_info()))
        handler.handle(_record("same", exc_info=key_error))

        assert inner.capture_exception.call_count == 2

    def test_seen_table_is_bounded(self):
        inner = MagicMock()
        clock = FakeClock()
        client = _client(inner, clock, events_per_minute=1e9, burst=1e9)
        for i in range(errors_module._MAX_TRACKED_FINGERPRINTS + 50):
            client.capture_message(logger_name="svc", template=f"m{i}", message="m")
            clock.advance(0.001)
        assert len(client._seen) <= errors_module._MAX_TRACKED_FINGERPRINTS
