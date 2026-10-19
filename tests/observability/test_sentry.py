"""Tests for wxyc_fastapi.observability.sentry."""

from unittest.mock import patch

from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

from wxyc_fastapi.observability.sentry import init_sentry


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

    def test_before_send_transaction_passed_through(self):
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
                before_send_transaction=my_filter,
            )
        assert mock_init.call_args.kwargs["before_send_transaction"] is my_filter

    def test_before_send_transaction_defaults_to_none(self):
        with (
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_init,
            patch("wxyc_fastapi.observability.sentry.sentry_sdk.set_tag"),
        ):
            init_sentry(dsn="https://test@sentry.example/1", service_name="test-service")
        assert mock_init.call_args.kwargs["before_send_transaction"] is None

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
