"""Shared fixtures for the observability test modules."""

import pytest

import wxyc_fastapi.observability.errors as errors_module
import wxyc_fastapi.observability.posthog as ph_module


@pytest.fixture(autouse=True)
def _reset_posthog_state():
    """Each test starts with a fresh client singleton + empty warn-once set.

    Both posthog test modules exercise the same module globals; one definition
    here (the repo's conftest convention, like the cache-stats ContextVar
    fixture) keeps the reset surface from drifting between per-file copies as
    the module grows state.
    """
    ph_module._client = None
    ph_module._warned_prefixes.clear()
    errors_module._client = None
    yield
    ph_module._client = None
    ph_module._warned_prefixes.clear()
    errors_module._client = None
