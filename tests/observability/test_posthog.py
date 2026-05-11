"""Tests for wxyc_fastapi.observability.posthog."""

from unittest.mock import MagicMock, patch

import pytest

import wxyc_fastapi.observability.posthog as ph_module
from wxyc_fastapi.observability.posthog import (
    flush_posthog,
    get_posthog_client,
    shutdown_posthog,
)


@pytest.fixture(autouse=True)
def _reset_posthog_state():
    """Each test starts with a fresh client + empty warn-once set."""
    ph_module._client = None
    ph_module._warned_prefixes.clear()
    yield
    ph_module._client = None
    ph_module._warned_prefixes.clear()


class TestGetPosthogClient:
    def test_returns_none_when_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        assert get_posthog_client("request") is None

    def test_returns_none_when_api_key_empty(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "")
        assert get_posthog_client("request") is None

    def test_warns_once_per_prefix(self, monkeypatch, caplog):
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        with caplog.at_level("WARNING"):
            get_posthog_client("request")
            get_posthog_client("request")
            get_posthog_client("request")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "POSTHOG_API_KEY" in warnings[0].message
        assert "request" in warnings[0].message

    def test_separate_warnings_for_different_prefixes(self, monkeypatch, caplog):
        monkeypatch.delenv("POSTHOG_API_KEY", raising=False)
        with caplog.at_level("WARNING"):
            get_posthog_client("request")
            get_posthog_client("lookup")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 2

    def test_constructs_client_with_api_key_and_default_host(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.delenv("POSTHOG_HOST", raising=False)

        with patch("posthog.Posthog") as MockPosthog:
            MockPosthog.return_value = MagicMock(name="ph_instance")
            client = get_posthog_client("request")
            MockPosthog.assert_called_once_with("phc_test123", host="https://us.i.posthog.com")
            assert client is MockPosthog.return_value

    def test_uses_custom_host_when_set(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        monkeypatch.setenv("POSTHOG_HOST", "https://eu.i.posthog.com")

        with patch("posthog.Posthog") as MockPosthog:
            get_posthog_client("request")
            MockPosthog.assert_called_once_with("phc_test123", host="https://eu.i.posthog.com")

    def test_returns_singleton_across_calls(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        with patch("posthog.Posthog") as MockPosthog:
            MockPosthog.return_value = MagicMock(name="ph_instance")
            a = get_posthog_client("request")
            b = get_posthog_client("lookup")
        assert a is b
        assert MockPosthog.call_count == 1


class TestFlush:
    def test_no_client_is_safe(self):
        # No client initialized — must not raise.
        flush_posthog()

    def test_flushes_existing_client(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        with patch("posthog.Posthog") as MockPosthog:
            instance = MagicMock(name="ph_instance")
            MockPosthog.return_value = instance
            get_posthog_client("request")
            flush_posthog()
        instance.flush.assert_called_once()


class TestShutdown:
    def test_no_client_is_safe(self):
        shutdown_posthog()

    def test_shuts_down_existing_client_and_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("POSTHOG_API_KEY", "phc_test123")
        with patch("posthog.Posthog") as MockPosthog:
            instance = MagicMock(name="ph_instance")
            MockPosthog.return_value = instance
            get_posthog_client("request")
            shutdown_posthog()
        instance.shutdown.assert_called_once()
        assert ph_module._client is None
