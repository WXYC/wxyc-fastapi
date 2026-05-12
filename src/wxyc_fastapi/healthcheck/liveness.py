"""Liveness router — returns ``{"status": "healthy"}`` without running any probes.

Mounted at ``GET /health``. Intended for orchestrators (Railway, Docker
``HEALTHCHECK``, ECS task health) that need an instant signal that the process
is up. For deeper "ready to serve traffic" checks, use :mod:`readiness`.
"""

from __future__ import annotations

from fastapi import APIRouter

liveness_router = APIRouter()


@liveness_router.get("/health")
async def liveness() -> dict[str, str]:
    return {"status": "healthy"}
