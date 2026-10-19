"""Tests for ``init_error_tracking``, its FastAPI handler, and the re-routed Sentry shims.

Singleton and warn-once state reset lives in this directory's conftest.py.
"""

import logging
import sys
import threading
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from posthog import contexts

import wxyc_fastapi.observability.error_tracking as et_module
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

    def test_does_not_touch_sentry(self, inner):
        _init()
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk") as sentry:
            capture_exception(ValueError("boom"))
        sentry.capture_exception.assert_not_called()


class TestAddBreadcrumbShim:
    def test_tags_the_active_context(self, inner):
        with contexts.new_context(), patch.object(contexts, "tag", wraps=contexts.tag) as tag:
            add_breadcrumb(category="discogs", message="search_releases", data={"q": "x"})
        tag.assert_called_once_with("breadcrumb.discogs", "search_releases")

    def test_is_dropped_without_an_active_context(self):
        add_breadcrumb(category="discogs", message="search_releases")  # must not raise

    def test_does_not_touch_sentry(self):
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk") as sentry:
            add_breadcrumb(category="discogs", message="m")
        sentry.add_breadcrumb.assert_not_called()
