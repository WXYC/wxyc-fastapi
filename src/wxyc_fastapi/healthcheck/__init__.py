"""Healthcheck primitives: liveness router + parameterized readiness router."""

from wxyc_fastapi.healthcheck.liveness import liveness_router
from wxyc_fastapi.healthcheck.readiness import (
    DEFAULT_TIMEOUT_SECONDS,
    Check,
    ProbeOutcome,
    ReadinessStatus,
    readiness_router,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "Check",
    "ProbeOutcome",
    "ReadinessStatus",
    "liveness_router",
    "readiness_router",
]
