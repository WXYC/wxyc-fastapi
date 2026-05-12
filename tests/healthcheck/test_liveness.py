"""Tests for wxyc_fastapi.healthcheck.liveness."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from wxyc_fastapi.healthcheck import liveness_router


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(liveness_router)
    return TestClient(app)


class TestLiveness:
    def test_returns_200_with_healthy_status(self):
        client = _make_client()
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_runs_no_probes_so_no_external_dependency(self):
        # The point of liveness is "the process is up" — it must not call out to
        # databases or APIs. Verified by absence of probe machinery; structural
        # check via the response shape (no `services` key).
        client = _make_client()
        body = client.get("/health").json()
        assert "services" not in body
