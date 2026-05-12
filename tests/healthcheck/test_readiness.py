"""Tests for wxyc_fastapi.healthcheck.readiness."""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wxyc_fastapi.healthcheck import Check, ReadinessResponse, readiness_router


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


def test_router_factory_returns_distinct_routers_per_call():
    # A factory that mutates module state across calls would surprise consumers
    # who instantiate two routers (e.g. for different mount points). Each call
    # should produce an independent APIRouter.
    r1 = readiness_router([])
    r2 = readiness_router([])
    assert r1 is not r2


class TestReadinessLogging:
    def test_probe_raise_logs_exception_with_probe_name(self, caplog):
        # When a probe fails, the operator needs to know *why* — silent
        # swallowing makes prod debugging impossible.
        client = _make_client([Check(name="db", probe=_raises, required=True)])
        with caplog.at_level("ERROR", logger="wxyc_fastapi.healthcheck.readiness"):
            client.get("/health/ready")
        assert any(
            "db" in record.message and record.exc_info is not None for record in caplog.records
        ), f"expected exc_info log mentioning 'db' probe, got {caplog.records!r}"

    def test_probe_timeout_logs_warning_with_probe_name_and_duration(self, caplog):
        client = _make_client(
            [Check(name="slow", probe=_slow_probe(0.1), required=True)],
            timeout=0.01,
        )
        with caplog.at_level("WARNING", logger="wxyc_fastapi.healthcheck.readiness"):
            client.get("/health/ready")
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("slow" in r.message and "0.010" in r.message for r in warnings), (
            f"expected timeout warning mentioning probe name and duration, got {warnings!r}"
        )

    def test_probe_returns_non_ok_logs_warning(self, caplog):
        async def returns_other() -> str:
            return "weird"

        client = _make_client([Check(name="weird", probe=returns_other, required=False)])
        with caplog.at_level("WARNING", logger="wxyc_fastapi.healthcheck.readiness"):
            client.get("/health/ready")
        assert any(
            "weird" in r.message and "non-ok" in r.message
            for r in caplog.records
            if r.levelname == "WARNING"
        ), f"expected non-ok warning, got {[r.message for r in caplog.records]!r}"


class TestReadinessOpenAPI:
    def test_openapi_schema_documents_readiness_response(self):
        # The Pydantic ReadinessResponse model on the route makes the schema
        # appear in /docs and in any auto-generated client (e.g. semantic-index
        # via openapi codegen).
        app = FastAPI()
        app.include_router(readiness_router([]))
        spec = app.openapi()
        path = spec["paths"]["/health/ready"]["get"]
        assert "responses" in path
        assert "200" in path["responses"]
        # ReadinessResponse should be referenced (FastAPI inlines via $ref)
        assert (
            "ReadinessResponse"
            in path["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        )

    def test_readiness_response_model_round_trips_a_real_payload(self):
        # Sanity that the model parses what the route emits.
        client = _make_client([Check(name="db", probe=_ok, required=True)])
        body = client.get("/health/ready").json()
        parsed = ReadinessResponse.model_validate(body)
        assert parsed.status == "healthy"
        assert parsed.services == {"db": "ok"}
