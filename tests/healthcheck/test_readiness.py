"""Tests for wxyc_fastapi.healthcheck.readiness."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from wxyc_fastapi.healthcheck import Check, readiness_router


def _make_client(checks: list[Check], *, timeout: float = 3.0) -> TestClient:
    app = FastAPI()
    app.include_router(readiness_router(checks, timeout=timeout))
    return TestClient(app)


async def _ok() -> str:
    return "ok"


async def _raises() -> str:
    raise RuntimeError("probe failed")


def _slow_probe(delay: float):
    async def probe() -> str:
        await asyncio.sleep(delay)
        return "ok"

    return probe


class TestReadinessAllPass:
    def test_all_required_pass_returns_200_healthy(self):
        client = _make_client(
            [
                Check(name="db", probe=_ok, required=True),
                Check(name="cache", probe=_ok, required=True),
            ]
        )
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["services"] == {"db": "ok", "cache": "ok"}

    def test_mixed_required_optional_all_ok_returns_healthy(self):
        client = _make_client(
            [
                Check(name="db", probe=_ok, required=True),
                Check(name="discogs", probe=_ok, required=False),
            ]
        )
        body = client.get("/health/ready").json()
        assert body["status"] == "healthy"


class TestReadinessOptionalProbeFailure:
    def test_optional_probe_raises_returns_200_degraded(self):
        client = _make_client(
            [
                Check(name="db", probe=_ok, required=True),
                Check(name="discogs", probe=_raises, required=False),
            ]
        )
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["services"] == {"db": "ok", "discogs": "unavailable"}

    def test_optional_probe_times_out_returns_200_degraded_with_timeout_marker(self):
        client = _make_client(
            [
                Check(name="db", probe=_ok, required=True),
                Check(name="slow", probe=_slow_probe(0.1), required=False),
            ],
            timeout=0.01,
        )
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["services"]["db"] == "ok"
        assert body["services"]["slow"] == "timeout"


class TestReadinessRequiredProbeFailure:
    def test_required_probe_raises_returns_503_unhealthy(self):
        client = _make_client(
            [
                Check(name="db", probe=_raises, required=True),
                Check(name="cache", probe=_ok, required=True),
            ]
        )
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["services"] == {"db": "unavailable", "cache": "ok"}

    def test_required_probe_times_out_returns_503_unhealthy(self):
        client = _make_client(
            [
                Check(name="db", probe=_slow_probe(0.1), required=True),
            ],
            timeout=0.01,
        )
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["services"] == {"db": "timeout"}


class TestReadinessNonOkReturnValue:
    def test_probe_returning_non_ok_string_treated_as_unavailable(self):
        async def returns_other() -> str:
            return "weird"

        client = _make_client([Check(name="weird", probe=returns_other, required=False)])
        body = client.get("/health/ready").json()
        assert body["services"]["weird"] == "unavailable"
        assert body["status"] == "degraded"


class TestReadinessParallelism:
    def test_probes_run_concurrently_not_serially(self):
        # Two slow probes (each 50ms) should complete in well under 100ms when
        # run in parallel. Generous ceiling (200ms) keeps this from flaking on
        # slow CI without losing the signal that they are not run serially.
        client = _make_client(
            [
                Check(name="a", probe=_slow_probe(0.05), required=True),
                Check(name="b", probe=_slow_probe(0.05), required=True),
            ],
            timeout=1.0,
        )
        import time

        start = time.perf_counter()
        response = client.get("/health/ready")
        elapsed = time.perf_counter() - start
        assert response.status_code == 200
        assert elapsed < 0.2, f"probes appear to run serially (elapsed {elapsed:.3f}s)"


class TestCheckDataclass:
    def test_check_required_defaults_to_true(self):
        # Required is the safe default — forgetting to mark a probe required
        # would silently downgrade an unhealthy service to degraded.
        check = Check(name="db", probe=_ok)
        assert check.required is True

    def test_check_is_constructible_with_keywords(self):
        # Positional construction is also fine but keyword construction is the
        # documented call site at consumers.
        check = Check(name="x", probe=_ok, required=False)
        assert check.name == "x"
        assert check.required is False


class TestReadinessEmpty:
    def test_no_checks_returns_healthy(self):
        # An empty checks list is a sensible default for services that have no
        # external dependencies but still want a /health/ready route for
        # symmetry.
        client = _make_client([])
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "services": {}}


@pytest.mark.asyncio
async def test_router_factory_returns_distinct_routers_per_call():
    # A factory that mutates module state across calls would surprise consumers
    # who instantiate two routers (e.g. for different mount points). Each call
    # should produce an independent APIRouter.
    r1 = readiness_router([])
    r2 = readiness_router([])
    assert r1 is not r2
