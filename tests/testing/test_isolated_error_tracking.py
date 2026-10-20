"""``isolated_error_tracking`` leaves no process-wide error-tracking state behind."""

import logging
import sys
import threading

import pytest

from wxyc_fastapi.observability import error_tracking, errors
from wxyc_fastapi.observability.error_tracking import init_error_tracking
from wxyc_fastapi.testing import CountingPosthog, isolated_error_tracking


def _state():
    return (
        list(logging.getLogger().handlers),
        sys.excepthook,
        threading.excepthook,
        error_tracking._hooks_installed,
        (errors._client, errors._config, errors._init_failed),
    )


def _init():
    return init_error_tracking(service="svc", environment="dev", daily_ceiling=100)


def test_logging_error_records_one_exception_on_the_stub():
    with isolated_error_tracking() as stub:
        assert _init() is not None
        logging.error("boom")
    assert stub.count("$exception") == 1


def test_caller_supplied_stub_is_yielded():
    mine = CountingPosthog()
    with isolated_error_tracking(mine) as stub:
        assert stub is mine


def test_state_is_restored_after_exit():
    before = _state()
    with isolated_error_tracking():
        _init()
        assert _state() != before
    assert _state() == before


def test_state_is_restored_when_the_body_raises():
    before = _state()
    with pytest.raises(RuntimeError), isolated_error_tracking():
        _init()
        raise RuntimeError("body failed")
    assert _state() == before


def test_a_preexisting_singleton_is_cleared_inside_and_restored_after(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(errors, "_client", sentinel)
    with isolated_error_tracking():
        assert errors._client is None
    assert errors._client is sentinel


def test_fixture_yields_a_stub(isolated_error_tracking_stub):
    assert _init() is not None
    logging.error("boom")
    assert isolated_error_tracking_stub.count("$exception") == 1
