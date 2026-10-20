"""Tests for ``init_error_tracking``, its FastAPI handler, and the ``capture_exception`` / ``add_breadcrumb`` shims.

Singleton and warn-once state reset lives in this directory's conftest.py.
"""

import asyncio
import importlib
import logging
import re
import sys
import threading
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from posthog import contexts

import wxyc_fastapi.observability as observability
import wxyc_fastapi.observability.error_tracking as et_module
import wxyc_fastapi.observability.errors as errors_module
from wxyc_fastapi.observability import add_breadcrumb, capture_exception
from wxyc_fastapi.observability.error_tracking import init_error_tracking
from wxyc_fastapi.observability.errors import (
    PosthogErrorsClient,
    PosthogErrorsHandler,
    get_posthog_errors_client,
)


@pytest.fixture
def inner(monkeypatch):
    """A mocked SDK client behind ``POSTHOG_ERRORS_API_KEY``, with process-wide hooks restored."""
    monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
    root = logging.getLogger()
    excepthook, thread_hook = sys.excepthook, threading.excepthook
    with patch("posthog.Posthog") as mock_cls:
        yield mock_cls.return_value
    for handler in [h for h in root.handlers if isinstance(h, PosthogErrorsHandler)]:
        root.removeHandler(handler)
    sys.excepthook, threading.excepthook = excepthook, thread_hook
    et_module._hooks_installed = False


def _init(**kwargs):
    kwargs.setdefault("service", "svc")
    kwargs.setdefault("environment", "production")
    kwargs.setdefault("daily_ceiling", 100)
    return init_error_tracking(**kwargs)


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/boom")
    async def boom():
        raise ValueError("kaboom")

    return app


class TestInit:
    def test_returns_none_without_key(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_ERRORS_API_KEY", raising=False)
        assert _init(app=_app()) is None

    def test_service_reaches_the_client(self, inner):
        client = _init(service="lml", app=_app())
        assert isinstance(client, PosthogErrorsClient)
        client.capture_message(logger_name="a", template="t", message="m")
        assert inner.capture.call_args.kwargs["properties"]["service"] == "lml"
        assert inner.capture.call_args.kwargs["distinct_id"] == "lml"

    def test_attaches_one_logging_handler_across_calls(self, inner):
        _init()
        _init()
        handlers = [h for h in logging.getLogger().handlers if isinstance(h, PosthogErrorsHandler)]
        assert len(handlers) == 1

    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("production", "production"),
            ("prod", "production"),
            (" Prod ", "production"),
            ("stage", "staging"),
            ("staging", "staging"),
            ("dev", "development"),
            ("local", "development"),
            ("development", "development"),
        ],
    )
    def test_environment_aliases_are_normalized(self, inner, given, expected):
        client = _init(environment=given)
        assert client._base_properties["environment"] == expected

    def test_unknown_environment_passes_through_with_one_warning(self, inner, caplog):
        with caplog.at_level(logging.WARNING, logger=et_module.__name__):
            client = _init(environment="qa")
            et_module._normalize_environment("qa")
        assert client._base_properties["environment"] == "qa"
        assert len([r for r in caplog.records if "qa" in r.getMessage()]) == 1

    @pytest.mark.parametrize(("release", "present"), [("1.2.3", True), (None, False)])
    def test_release_is_stamped_only_when_given(self, inner, release, present):
        client = _init(release=release)
        client.capture_message(logger_name="a", template="t", message="m")
        props = inner.capture.call_args.kwargs["properties"]
        assert ("release" in props) is present
        if present:
            assert props["release"] == release

    def test_before_send_reaches_the_sdk(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        hook = lambda event: event  # noqa: E731
        with patch("posthog.Posthog") as mock_cls:
            _init(before_send=hook)
        assert mock_cls.call_args.kwargs["before_send"] is hook

    def test_daily_ceiling_is_required(self, inner):
        with pytest.raises(TypeError):
            init_error_tracking(service="svc", environment="production")  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "changed",
        [{"service": "other"}, {"environment": "staging"}, {"daily_ceiling": 50}],
    )
    def test_differing_arguments_warn_once_and_first_call_wins(self, inner, caplog, changed):
        first = _init()
        with caplog.at_level(logging.WARNING, logger="wxyc_fastapi.observability.errors"):
            second = _init(**changed)
            _init(**changed)
        assert second is first
        warnings = [r for r in caplog.records if "ignored" in r.getMessage()]
        assert len(warnings) == 1

    def test_identical_arguments_do_not_warn(self, inner, caplog):
        _init()
        with caplog.at_level(logging.WARNING, logger="wxyc_fastapi.observability.errors"):
            _init()
        assert not caplog.records


class TestExceptionHandler:
    def test_unhandled_route_exception_reports_once_and_returns_500(self, inner):
        app = _app()
        _init(app=app)
        client = TestClient(app, raise_server_exceptions=False)

        response = client.get("/boom")

        assert response.status_code == 500
        assert inner.capture_exception.call_count == 1
        assert inner.capture_exception.call_args.args[0][0] is ValueError

    def test_the_500_echoes_the_request_id_the_event_carries(self, inner):
        app = _app()
        _init(app=app)
        response = TestClient(app, raise_server_exceptions=False).get("/boom")
        event_id = inner.capture_exception.call_args.kwargs["properties"]["request_id"]
        assert response.headers["X-Request-ID"] == event_id

    def test_the_server_logging_the_reraised_exception_does_not_report_twice(self, inner):
        app = _app()
        _init(app=app)
        logger = logging.getLogger("uvicorn.error")

        def reraise_and_log():
            # Starlette re-raises after the handler; uvicorn then logs it at ERROR.
            try:
                TestClient(app).get("/boom")
            except ValueError:
                logger.error("Exception in ASGI application", exc_info=True)

        reraise_and_log()

        assert inner.capture_exception.call_count == 1

    def test_unmarked_log_exceptions_are_still_reported(self, inner):
        _init()
        try:
            raise ValueError("elsewhere")
        except ValueError:
            logging.getLogger("svc").error("failed", exc_info=True)
        assert inner.capture_exception.call_count == 1

    def test_raising_sdk_call_does_not_propagate(self, inner):
        inner.capture_exception.side_effect = RuntimeError("sdk down")
        app = _app()
        _init(app=app)
        response = TestClient(app, raise_server_exceptions=False).get("/boom")
        assert response.status_code == 500


class _BadStrError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no str")


class TestRaisingStr:
    def test_excepthook_still_chains_and_sends(self, inner):
        calls = []
        original = sys.excepthook
        sys.excepthook = lambda *a: calls.append(a)
        try:
            _init()
            try:
                raise _BadStrError()
            except _BadStrError:
                sys.excepthook(*sys.exc_info())
        finally:
            sys.excepthook = original
        assert len(calls) == 1
        assert inner.capture_exception.call_count == 1

    def test_shim_does_not_raise_and_still_sends(self, inner):
        _init()
        capture_exception(_BadStrError())
        assert inner.capture_exception.call_count == 1


class TestExcepthooks:
    def test_uncaught_exceptions_go_through_the_gated_client(self, inner):
        _init()
        try:
            raise ValueError("uncaught")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        assert inner.capture_exception.call_count == 1

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_thread_exceptions_go_through_the_gated_client(self, inner):
        _init()

        def work():
            raise ValueError("in thread")

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        assert inner.capture_exception.call_count == 1

    def test_hooks_are_installed_once(self, inner):
        _init()
        hook = sys.excepthook
        _init()
        assert sys.excepthook is hook


class TestClientContract:
    @pytest.mark.parametrize("method", ["capture_exception", "capture_message"])
    def test_raising_sdk_call_does_not_propagate(self, inner, method):
        inner.capture_exception.side_effect = RuntimeError("sdk down")
        inner.capture.side_effect = RuntimeError("sdk down")
        client = get_posthog_errors_client(
            service="svc", environment="production", daily_ceiling=100
        )
        if method == "capture_exception":
            try:
                raise ValueError("x")
            except ValueError:
                client.capture_exception(sys.exc_info(), logger_name="a", template="t")
        else:
            client.capture_message(logger_name="a", template="t", message="m")

    @pytest.mark.parametrize(
        ("version", "sets_level"),
        [("7.65.0rc1", False), ("7.64.0rc1", True), ("garbage", True), ("", True)],
    )
    def test_level_for_prerelease_and_unknown_sdk_versions(self, inner, version, sets_level):
        with patch("posthog.version.VERSION", version):
            client = _init()
        try:
            raise ValueError("x")
        except ValueError:
            client.capture_exception(sys.exc_info(), logger_name="a", template="t")
        props = inner.capture_exception.call_args.kwargs["properties"]
        assert ("$exception_level" in props) is sets_level

    def test_unreadable_sdk_version_sets_level(self, inner):
        with patch.dict(sys.modules, {"posthog.version": None}):
            client = _init()
        assert client._sdk_omits_level is True


class TestShimExports:
    def test_shims_live_in_errors_and_are_exported_from_the_package(self):
        assert errors_module.capture_exception is capture_exception
        assert errors_module.add_breadcrumb is add_breadcrumb

    def test_sentry_is_gone(self):
        with pytest.raises(ImportError):
            importlib.import_module("wxyc_fastapi.observability.sentry")
        assert not hasattr(observability, "init_sentry")


class TestCaptureExceptionShim:
    def test_routes_to_the_errors_client_with_context_as_properties(self, inner):
        _init()
        err = ValueError("boom")
        capture_exception(err, {"q": "stereolab"}, context_name="discogs")
        call = inner.capture_exception.call_args
        assert call.args[0][1] is err
        assert call.kwargs["properties"]["discogs"] == {"q": "stereolab"}
        assert call.kwargs["properties"]["service"] == "svc"

    def test_default_context_name_is_request(self, inner):
        _init()
        capture_exception(ValueError("boom"), {"k": "v"})
        assert inner.capture_exception.call_args.kwargs["properties"]["request"] == {"k": "v"}

    def test_is_a_noop_before_init(self, inner):
        capture_exception(ValueError("boom"))
        inner.capture_exception.assert_not_called()


class TestAddBreadcrumbShim:
    def test_tags_the_active_context(self, inner):
        with contexts.new_context(), patch.object(contexts, "tag", wraps=contexts.tag) as tag:
            add_breadcrumb(category="discogs", message="search_releases", data={"q": "x"})
        tag.assert_called_once_with("breadcrumb.discogs", "search_releases")

    def test_is_dropped_without_an_active_context(self):
        add_breadcrumb(category="discogs", message="search_releases")  # must not raise


def _routes(app: FastAPI) -> FastAPI:
    @app.get("/log-then-raise")
    async def log_then_raise():
        try:
            raise ValueError("logged")
        except ValueError:
            logging.getLogger("svc.route").exception("lookup failed")
            raise

    @app.get("/capture-then-raise")
    async def capture_then_raise():
        try:
            raise ValueError("captured")
        except ValueError as err:
            capture_exception(err, {"k": 1})
            raise

    return app


class TestExactlyOnce:
    @pytest.mark.parametrize("path", ["/boom", "/log-then-raise", "/capture-then-raise"])
    def test_one_event_per_unhandled_route_exception(self, inner, path):
        app = _routes(_app())
        _init(app=app)
        client = TestClient(app, raise_server_exceptions=False)
        client.get(path)
        assert inner.capture_exception.call_count == 1

    def test_dedupe_dropped_capture_does_not_mark_the_exception(self, inner):
        client = _init(app=_app())
        first, second = ValueError("same"), ValueError("same")
        for err in (first, second):
            client.capture_exception(
                (ValueError, err, None), logger_name="a", template="t", dedupe_on_message=True
            )
        assert inner.capture_exception.call_count == 1
        assert getattr(second, "_wxyc_error_reported", False) is False
        assert getattr(first, "_wxyc_error_reported", False) is True


class TestRequestId:
    @pytest.fixture
    def app(self, inner):
        app = _routes(_app())
        _init(app=app)
        return app

    def _event_request_id(self, inner):
        return inner.capture_exception.call_args.kwargs["properties"].get("request_id")

    @pytest.mark.parametrize("path", ["/boom", "/log-then-raise", "/capture-then-raise"])
    def test_valid_header_passes_through_and_is_stamped(self, inner, app, path):
        response = TestClient(app, raise_server_exceptions=False).get(
            path, headers={"X-Request-ID": "abc-123_4.5"}
        )
        assert self._event_request_id(inner) == "abc-123_4.5"
        if path != "/boom":
            return
        assert response.status_code == 500

    def test_header_is_echoed_on_the_response(self, app):
        @app.get("/ok")
        async def ok():
            return {}

        response = TestClient(app).get("/ok", headers={"X-Request-ID": "abc"})
        assert response.headers["X-Request-ID"] == "abc"

    def test_missing_header_gets_a_generated_id(self, app):
        @app.get("/ok")
        async def ok():
            return {}

        response = TestClient(app).get("/ok")
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])

    @pytest.mark.parametrize("bad", ["a b", "x" * 129, "a/b", "<script>"])
    def test_invalid_header_is_replaced(self, inner, app, bad):
        TestClient(app, raise_server_exceptions=False).get("/boom", headers={"X-Request-ID": bad})
        assert re.fullmatch(r"[0-9a-f]{32}", self._event_request_id(inner))

    def test_omitted_outside_a_request(self, inner):
        client = _init()
        try:
            raise ValueError("x")
        except ValueError:
            client.capture_exception(sys.exc_info(), logger_name="a", template="t")
        client.capture_message(logger_name="a", template="t", message="m")
        assert "request_id" not in inner.capture_exception.call_args.kwargs["properties"]
        assert "request_id" not in inner.capture.call_args.kwargs["properties"]


class TestMechanismAndProperties:
    def _raise_into(self, hook):
        try:
            raise ValueError("crash")
        except ValueError:
            hook(*sys.exc_info())

    def _metadata(self, inner):
        return inner.capture_exception.call_args.kwargs.get("_capture_metadata")

    def test_process_excepthook_is_unhandled(self, inner):
        _init()
        self._raise_into(sys.excepthook)
        assert self._metadata(inner)["mechanism"] == {"type": "excepthook", "handled": False}

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_thread_excepthook_is_unhandled(self, inner):
        _init()

        def work():
            raise ValueError("in thread")

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()
        assert self._metadata(inner)["mechanism"] == {
            "type": "threading.excepthook",
            "handled": False,
        }

    def test_route_handler_is_unhandled_starlette(self, inner):
        app = _app()
        _init(app=app)
        TestClient(app, raise_server_exceptions=False).get("/boom")
        assert self._metadata(inner)["mechanism"] == {"type": "starlette", "handled": False}
        assert inner.capture_exception.call_args.kwargs["properties"]["logger"] == "fastapi"

    def test_old_sdk_gets_no_capture_metadata(self, inner):
        with patch("posthog.version.VERSION", "7.64.0"):
            _init()
        self._raise_into(sys.excepthook)
        assert "_capture_metadata" not in inner.capture_exception.call_args.kwargs

    def test_shim_and_log_events_carry_the_logger_name(self, inner):
        _init()
        capture_exception(ValueError("a"))
        assert (
            inner.capture_exception.call_args.kwargs["properties"]["logger"] == "capture_exception"
        )
        try:
            raise ValueError("b")
        except ValueError:
            logging.getLogger("svc.x").exception("failed")
        assert inner.capture_exception.call_args.kwargs["properties"]["logger"] == "svc.x"


class TestDedupeKeys:
    def test_same_type_different_messages_are_both_reported(self, inner):
        _init()
        capture_exception(ValueError("one"))
        capture_exception(ValueError("two"))
        assert inner.capture_exception.call_count == 2

    def test_same_type_and_message_is_deduped(self, inner):
        _init()
        capture_exception(ValueError("one"))
        capture_exception(ValueError("one"))
        assert inner.capture_exception.call_count == 1

    def test_the_message_is_truncated_in_the_key(self, inner):
        client = _init()
        capture_exception(ValueError("x" * 500))
        assert all(len(k) < 300 for k in client._seen)


class TestBreadcrumbIsolation:
    def test_tag_reaches_the_event_and_stays_in_its_request(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        events: list[dict] = []

        def keep(event):
            events.append(event)
            return None  # drop: nothing leaves the process

        app = FastAPI()
        gate = {"a": asyncio.Event(), "b": asyncio.Event()}

        @app.get("/{name}")
        async def route(name: str):
            with contexts.new_context():
                add_breadcrumb(category="step", message=f"crumb-{name}")
                gate[name].set()
                await asyncio.wait_for(gate["a"].wait(), 5)
                await asyncio.wait_for(gate["b"].wait(), 5)
                capture_exception(ValueError(name))
            return {}

        async def run():
            _init(app=app, before_send=keep)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await asyncio.gather(c.get("/a"), c.get("/b"))

        try:
            asyncio.run(run())
        finally:
            for handler in [
                h for h in logging.getLogger().handlers if isinstance(h, PosthogErrorsHandler)
            ]:
                logging.getLogger().removeHandler(handler)
            from wxyc_fastapi.observability import shutdown_posthog_errors

            shutdown_posthog_errors()
            et_module._hooks_installed = False
        by_value = {e["properties"]["$exception_list"][0]["value"]: e for e in events}
        assert set(by_value) == {"a", "b"}
        for name, event in by_value.items():
            tags = {k: v for k, v in event["properties"].items() if k.startswith("breadcrumb.")}
            assert tags == {"breadcrumb.step": f"crumb-{name}"}


class TestRequestContext:
    """The middleware opens the posthog context, so routes never have to."""

    def _run(self, monkeypatch, app, paths):
        monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_errors")
        events: list[dict] = []

        def keep(event):
            events.append(event)
            return None  # drop: nothing leaves the process

        async def run():
            _init(app=app, before_send=keep)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                await asyncio.gather(*(c.get(p, headers={"X-Request-ID": p[1:]}) for p in paths))

        try:
            asyncio.run(run())
        finally:
            for handler in [
                h for h in logging.getLogger().handlers if isinstance(h, PosthogErrorsHandler)
            ]:
                logging.getLogger().removeHandler(handler)
            from wxyc_fastapi.observability import shutdown_posthog_errors

            shutdown_posthog_errors()
            et_module._hooks_installed = False
        return events

    def test_breadcrumb_then_error_log_carries_the_tag(self, monkeypatch):
        app = FastAPI()

        @app.get("/log")
        async def route():
            add_breadcrumb(category="discogs", message="search_releases")
            logging.getLogger("svc.route").error("lookup failed")
            return {}

        (event,) = self._run(monkeypatch, app, ["/log"])
        assert event["properties"]["breadcrumb.discogs"] == "search_releases"

    def test_concurrent_requests_carry_only_their_own_tags(self, monkeypatch):
        app = FastAPI()
        gate = {"a": asyncio.Event(), "b": asyncio.Event()}

        @app.get("/{name}")
        async def route(name: str):
            add_breadcrumb(category="step", message=f"crumb-{name}")
            gate[name].set()
            await asyncio.wait_for(gate["a"].wait(), 5)
            await asyncio.wait_for(gate["b"].wait(), 5)
            capture_exception(ValueError(name))
            return {}

        events = self._run(monkeypatch, app, ["/a", "/b"])
        by_value = {e["properties"]["$exception_list"][0]["value"]: e for e in events}
        assert set(by_value) == {"a", "b"}
        for name, event in by_value.items():
            props = event["properties"]
            tags = {k: v for k, v in props.items() if k.startswith("breadcrumb.")}
            assert tags == {"breadcrumb.step": f"crumb-{name}"}
            assert props["request_id"] == name

    def test_route_exception_is_sent_once(self, monkeypatch):
        events = self._run(monkeypatch, _app(), ["/boom"])
        assert len(events) == 1

    def test_route_crash_carries_the_request_breadcrumbs(self, monkeypatch):
        app = FastAPI()

        @app.get("/crash")
        async def route():
            add_breadcrumb(category="discogs", message="search_releases")
            raise ValueError("crash")

        (event,) = self._run(monkeypatch, app, ["/crash"])
        props = event["properties"]
        assert props["breadcrumb.discogs"] == "search_releases"
        assert props["request_id"] == "crash"
        mechanism = props["$exception_list"][0]["mechanism"]
        assert (mechanism["type"], mechanism["handled"]) == ("starlette", False)
