"""Tests for wxyc_fastapi.observability.sentry."""

from unittest.mock import patch

from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

from wxyc_fastapi.observability.sentry import (
    add_breadcrumb,
    capture_exception,
    init_sentry,
)


class TestInitSentry:
    def test_skips_init_when_dsn_is_none(self):
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init:
            init_sentry(dsn=None, service_name="test-service")
            mock_init.assert_not_called()

    def test_skips_init_when_dsn_is_empty_string(self):
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init:
            init_sentry(dsn="", service_name="test-service")
            mock_init.assert_not_called()

    def test_default_integrations_include_fastapi_and_httpx(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(dsn="https://test@sentry.example/1", service_name="test-service")
        kwargs = mock_init.call_args.kwargs
        integration_types = {type(i) for i in kwargs["integrations"]}
        assert FastApiIntegration in integration_types
        assert HttpxIntegration in integration_types

    def test_release_tag_passed_through(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(
                dsn="https://test@sentry.example/1",
                service_name="test-service",
                release="v1.2.3",
            )
        assert mock_init.call_args.kwargs["release"] == "v1.2.3"

    def test_environment_tag_passed_through(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(
                dsn="https://test@sentry.example/1",
                service_name="test-service",
                environment="staging",
            )
        assert mock_init.call_args.kwargs["environment"] == "staging"

    def test_environment_defaults_to_local(self):
        # Matches the rom convention so a stray init call doesn't pollute prod.
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(dsn="https://test@sentry.example/1", service_name="test-service")
        assert mock_init.call_args.kwargs["environment"] == "local"

    def test_service_name_tag_set(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init"),
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag") as mock_tag,
        ):
            init_sentry(
                dsn="https://test@sentry.example/1",
                service_name="library-metadata-lookup",
            )
        mock_tag.assert_any_call("service.name", "library-metadata-lookup")

    def test_before_send_filter_passed_through(self):
        called = []

        def my_filter(event, hint):
            called.append((event, hint))
            return event

        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(
                dsn="https://test@sentry.example/1",
                service_name="test-service",
                before_send=my_filter,
            )
        assert mock_init.call_args.kwargs["before_send"] is my_filter

    def test_caller_can_override_integrations(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(
                dsn="https://test@sentry.example/1",
                service_name="test-service",
                integrations=[FastApiIntegration()],  # explicitly omit Httpx
            )
        integration_types = {type(i) for i in mock_init.call_args.kwargs["integrations"]}
        assert HttpxIntegration not in integration_types
        assert FastApiIntegration in integration_types

    def test_default_sample_rates_are_full(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(dsn="https://test@sentry.example/1", service_name="test-service")
        kwargs = mock_init.call_args.kwargs
        assert kwargs["traces_sample_rate"] == 1.0
        assert kwargs["sample_rate"] == 1.0


class TestAddBreadcrumb:
    def test_passes_category_and_message(self):
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk.add_breadcrumb") as mock_bc:
            add_breadcrumb(category="discogs", message="search_releases", data={"q": "stereolab"})
        mock_bc.assert_called_once_with(
            category="discogs",
            message="search_releases",
            data={"q": "stereolab"},
            level="info",
        )

    def test_data_defaults_to_empty_dict(self):
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk.add_breadcrumb") as mock_bc:
            add_breadcrumb(category="groq", message="parse")
        assert mock_bc.call_args.kwargs["data"] == {}

    def test_level_passes_through(self):
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk.add_breadcrumb") as mock_bc:
            add_breadcrumb(category="slack", message="post", level="warning")
        assert mock_bc.call_args.kwargs["level"] == "warning"


class TestCaptureException:
    def test_captures_exception(self):
        err = ValueError("boom")
        with patch("wxyc_fastapi.observability.sentry.sentry_sdk.capture_exception") as mock_cap:
            capture_exception(err)
        mock_cap.assert_called_once_with(err)

    def test_attaches_context_when_provided(self):
        err = ValueError("boom")
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_context") as mock_ctx,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.capture_exception"),
        ):
            capture_exception(err, context={"q": "stereolab"}, context_name="discogs")
        mock_ctx.assert_called_once_with("discogs", {"q": "stereolab"})

    def test_default_context_name(self):
        err = ValueError("boom")
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_context") as mock_ctx,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.capture_exception"),
        ):
            capture_exception(err, context={"k": "v"})
        # Default context name is "request" — generic, consumer-agnostic.
        mock_ctx.assert_called_once_with("request", {"k": "v"})
