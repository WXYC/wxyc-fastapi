"""Tests for the PostHog errors client and ERROR-to-exception logging handler.

Singleton state reset lives in this directory's conftest.py.
"""

import atexit
import logging
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

import wxyc_fastapi.observability.errors as errors_module
from wxyc_fastapi.observability.errors import (
    ERRORS_BURST,
    ERRORS_DEDUPE_WINDOW_SECONDS,
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
    kwargs.setdefault("service", "svc")
    kwargs.setdefault("environment", "production")
    kwargs.setdefault("daily_ceiling", 300)
    return PosthogErrorsClient(inner, clock=clock or FakeClock(), **kwargs)


def _get(**kwargs):
    kwargs.setdefault("service", "svc")
    kwargs.setdefault("environment", "production")
    kwargs.setdefault("daily_ceiling", 300)
    return get_posthog_errors_client(**kwargs)


def _exc_info():
    try:
        raise ValueError("kaboom")
    except ValueError:
        return sys.exc_info()


class TestGetPosthogErrorsClient:
    def test_none_when_key_unset(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_ERRORS_API_KEY", raising=False)
        assert _get() is None

    def test_none_when_only_analytics_key_set(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_ERRORS_API_KEY", raising=False)
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_analytics")
        assert _get() is None

    def test_constructs_with_errors_key_and_code_variables_off(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_analytics")
        monkeypatch.delenv("POSTHOG_HOST", raising=False)
        with patch("posthog.Posthog") as mock_cls:
            client = _get()
        assert isinstance(client, PosthogErrorsClient)
        args, kwargs = mock_cls.call_args
        assert args == ("phc_errors",)
        assert kwargs["host"] == "https://us.i.posthog.com"
        assert kwargs["capture_exception_code_variables"] is False

    def test_import_failure_returns_none_warns_once_and_is_cached(self, monkeypatch, caplog):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        monkeypatch.setitem(sys.modules, "posthog", None)  # makes ``from posthog import ...`` raise
        with caplog.at_level(logging.WARNING, logger=errors_module.__name__):
            assert _get() is None
            assert _get() is None
        assert len(caplog.records) == 1

    def test_constructor_failure_returns_none_warns_once_and_does_not_retry(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog", side_effect=RuntimeError("bad config")) as mock_cls:
            with caplog.at_level(logging.WARNING, logger=errors_module.__name__):
                assert _get() is None
                assert _get() is None
        assert mock_cls.call_count == 1
        assert len(caplog.records) == 1

    def test_passes_service_environment_and_ceiling_to_client(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog"):
            client = _get(service="lml", environment="staging", daily_ceiling=864)
        assert client._service == "lml"
        assert client._base_properties["environment"] == "staging"
        assert client._rate_per_second == pytest.approx(0.01)

    def test_is_a_singleton_independent_of_analytics_client(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog"):
            assert _get() is _get()

    def test_shutdown_shuts_inner_down_and_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog") as mock_cls:
            first = _get()
            shutdown_posthog_errors()
            mock_cls.return_value.shutdown.assert_called_once()
            assert _get() is not first

    def test_building_the_client_registers_one_atexit_shutdown(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with patch("posthog.Posthog") as mock_cls, patch.object(atexit, "register") as register:
            _get()
            _get()
        register.assert_called_once_with(shutdown_posthog_errors)
        register.call_args.args[0]()
        mock_cls.return_value.shutdown.assert_called_once()

    def test_rebuilding_after_shutdown_reregisters_the_hook_after_the_sdk_client_is_built(
        self, monkeypatch
    ):
        # atexit is LIFO: the wxyc hook must be registered after the SDK client's own join hook
        # so it runs first. A rebuild therefore unregisters and re-registers it, leaving one live.
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        events: list[str] = []
        with (
            patch(
                "posthog.Posthog", side_effect=lambda *a, **k: events.append("sdk") or MagicMock()
            ),
            patch.object(
                atexit, "register", side_effect=lambda f: events.append(f"register:{f.__name__}")
            ),
            patch.object(
                atexit,
                "unregister",
                side_effect=lambda f: events.append(f"unregister:{f.__name__}"),
            ),
        ):
            _get()
            shutdown_posthog_errors()
            _get()
        hook = shutdown_posthog_errors.__name__
        assert events[-3:] == ["sdk", f"unregister:{hook}", f"register:{hook}"]
        live = 0
        for e in events:
            if e == f"register:{hook}":
                live += 1
            elif e == f"unregister:{hook}":
                live = max(live - 1, 0)
        assert live == 1

    def test_at_interpreter_exit_the_hook_runs_before_a_rebuilt_clients_sdk_join(self, tmp_path):
        # Real atexit in a child process, with a stand-in SDK that registers its join hook on
        # construction the way posthog.Client does. No network. (Installed SDKs differ on whether
        # a join-first order hangs or merely drops events, so the order itself is asserted.)
        script = tmp_path / "child.py"
        script.write_text(
            "import atexit, sys, types\n"
            "order = []\n"
            "class Posthog:\n"
            "    def __init__(self, *a, **k):\n"
            "        atexit.register(self.join)\n"
            "    def join(self):\n"
            "        order.append('sdk-join')\n"
            "    def flush(self):\n"
            "        order.append('flush')\n"
            "    def shutdown(self):\n"
            "        order.append('shutdown')\n"
            "sys.modules['posthog'] = types.SimpleNamespace(Posthog=Posthog)\n"
            "atexit.register(lambda: print(','.join(order)))\n"
            "from wxyc_fastapi.observability.errors import get_posthog_errors_client as g, "
            "shutdown_posthog_errors as sd\n"
            "g(service='s', environment='e', daily_ceiling=300)\n"
            "sd()\n"
            "order.clear()\n"
            "g(service='s', environment='e', daily_ceiling=300)\n"
        )
        env = {**os.environ, "POSTHOG_ERRORS_API_KEY": "phc_test"}
        result = subprocess.run(
            [sys.executable, str(script)], env=env, timeout=20, capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        steps = result.stdout.strip().split(",")
        assert steps.index("shutdown") < steps.index("sdk-join"), steps

    def test_a_failed_build_registers_no_hook(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        with (
            patch("posthog.Posthog", side_effect=RuntimeError("bad")),
            patch.object(atexit, "register") as register,
        ):
            assert _get() is None
        register.assert_not_called()

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
                "type": "Message",
                "value": "lookup failed for Juana Molina",
                "mechanism": {"type": "generic", "handled": True, "synthetic": True},
            }
        ]
        assert props["logger"] == "svc.lookup"
        assert props["$exception_level"] == "error"

    def test_fingerprint_uses_unformatted_template(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner))

        handler.handle(_record("lookup failed for %s", ("Juana Molina",)))
        handler.handle(_record("lookup failed for %s", ("Stereolab",)))

        fingerprints = [
            c.kwargs["properties"]["$exception_fingerprint"] for c in inner.capture.call_args_list
        ]
        # The second record is deduped, but both would share this fingerprint.
        # PostHog types $exception_fingerprint as Option<String>; a list is dropped.
        assert fingerprints == ["svc.lookup:lookup failed for %s"]

    def test_empty_exc_info_tuple_synthesizes_exception_event(self):
        inner = MagicMock()
        PosthogErrorsHandler(_client(inner)).handle(_record(exc_info=(None, None, None)))
        inner.capture_exception.assert_not_called()
        inner.capture.assert_called_once()

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


class TestEventProperties:
    def test_both_paths_stamp_identity_properties_and_use_service_as_distinct_id(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner, service="lml", environment="staging"))
        handler.handle(_record("a"))
        handler.handle(_record("b", exc_info=_exc_info()))
        for call in (inner.capture.call_args, inner.capture_exception.call_args):
            assert call.kwargs["distinct_id"] == "lml"
            props = call.kwargs["properties"]
            assert props["service"] == "lml"
            assert props["environment"] == "staging"
            assert props["$process_person_profile"] is False

    def test_distinct_id_constant_is_gone(self):
        assert not hasattr(errors_module, "_DISTINCT_ID")

    @pytest.mark.parametrize(
        ("version", "sets_level"),
        [("7.64.9", True), ("6.9.0", True), ("7.65.0", False), ("7.72.0", False)],
    )
    def test_exception_path_sets_level_only_below_7_65_0(self, version, sets_level):
        inner = MagicMock()
        with patch("posthog.version.VERSION", version):
            client = _client(inner)
        PosthogErrorsHandler(client).handle(_record(exc_info=_exc_info()))
        props = inner.capture_exception.call_args.kwargs["properties"]
        assert ("$exception_level" in props) is sets_level
        if sets_level:
            assert props["$exception_level"] == "error"


class TestReentrancy:
    @pytest.mark.parametrize(
        "name",
        [
            "posthog",
            "posthog.client",
            "wxyc_fastapi.observability",
            "wxyc_fastapi.observability.posthog",
            "wxyc_fastapi.observability.errors",
        ],
    )
    def test_own_logger_output_is_not_captured(self, name):
        inner = MagicMock()
        PosthogErrorsHandler(_client(inner)).handle(_record(name=name))
        inner.capture.assert_not_called()
        inner.capture_exception.assert_not_called()

    @pytest.mark.parametrize(
        "name",
        [
            "wxyc_fastapi",
            "wxyc_fastapi.healthcheck.readiness",
            "wxyc_fastapi.observability_extras",
            "posthog_helpers",
        ],
    )
    def test_other_package_loggers_are_captured(self, name):
        inner = MagicMock()
        PosthogErrorsHandler(_client(inner)).handle(_record(name=name, exc_info=_exc_info()))
        inner.capture_exception.assert_called_once()

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
        assert ERRORS_BURST == 20
        assert ERRORS_DEDUPE_WINDOW_SECONDS > 0

    def test_drops_beyond_burst_across_both_paths(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner, daily_ceiling=86400 * 60, burst=2))

        handler.handle(_record("a"))
        handler.handle(_record("b", exc_info=_exc_info()))
        handler.handle(_record("c"))

        assert inner.capture.call_count + inner.capture_exception.call_count == 2

    def test_refill_admits_later_errors(self):
        inner = MagicMock()
        clock = FakeClock()
        handler = PosthogErrorsHandler(_client(inner, clock, daily_ceiling=86400 * 60, burst=1))

        handler.handle(_record("a"))
        handler.handle(_record("b"))  # dropped
        clock.advance(1.0)
        handler.handle(_record("c"))

        assert inner.capture.call_count == 2

    def test_dropped_records_do_not_consume_tokens_via_dedupe(self):
        inner = MagicMock()
        handler = PosthogErrorsHandler(_client(inner, daily_ceiling=86400 * 60, burst=2))

        handler.handle(_record("same"))
        handler.handle(_record("same"))  # deduped, costs no token
        handler.handle(_record("other"))

        assert inner.capture.call_count == 2


class TestBudget:
    def test_defaults_admit_twenty_then_one_token_per_ceiling_interval(self):
        inner = MagicMock()
        clock = FakeClock()
        client = _client(inner, clock, daily_ceiling=300)  # one token every 288 s
        for i in range(25):
            client.capture_message(logger_name="svc", template=f"m{i}", message="m")
        assert inner.capture.call_count == 20

        clock.advance(287)
        client.capture_message(logger_name="svc", template="late", message="m")
        assert inner.capture.call_count == 20
        clock.advance(1.5)
        client.capture_message(logger_name="svc", template="late", message="m")
        assert inner.capture.call_count == 21

    def test_repeat_inside_dedupe_window_costs_no_token(self):
        inner = MagicMock()
        client = _client(inner, daily_ceiling=300)
        for _ in range(50):
            client.capture_message(logger_name="svc", template="same", message="m")
        for i in range(19):
            client.capture_message(logger_name="svc", template=f"m{i}", message="m")
        assert inner.capture.call_count == 20


class TestDropLogging:
    def test_drops_log_one_rate_limited_warning(self, caplog):
        inner = MagicMock()
        clock = FakeClock()
        client = _client(inner, clock, daily_ceiling=86400 * 60, burst=1)
        with caplog.at_level(logging.WARNING, logger=errors_module.__name__):
            client.capture_message(logger_name="svc", template="a", message="a")
            client.capture_message(logger_name="svc", template="a", message="a")  # deduped
            client.capture_message(logger_name="svc", template="b", message="b")  # no token
            assert len(caplog.records) == 1
            clock.advance(errors_module._DROP_LOG_INTERVAL_SECONDS + 1)
            client.capture_message(logger_name="svc", template="a", message="a")  # admitted
            client.capture_message(logger_name="svc", template="c", message="c")  # no token
        assert len(caplog.records) == 2
        assert all(r.levelno == logging.WARNING for r in caplog.records)
        assert "dropped" in caplog.records[0].getMessage()


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
        client = _client(inner, clock, daily_ceiling=1e9, burst=1e9)
        for i in range(errors_module._MAX_TRACKED_FINGERPRINTS + 50):
            client.capture_message(logger_name="svc", template=f"m{i}", message="m")
            clock.advance(0.001)
        assert len(client._seen) <= errors_module._MAX_TRACKED_FINGERPRINTS
