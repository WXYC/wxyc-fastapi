"""Readiness router — runs probes in parallel and aggregates a status.

Mounted at ``GET /health/ready``. Each consumer supplies a list of
:class:`Check` instances; the router runs every probe concurrently with a
per-check timeout and returns:

* ``healthy`` — all probes returned ``"ok"``.
* ``degraded`` — every required probe returned ``"ok"`` and at least one
  optional probe failed or timed out. HTTP 200 (the service is still
  servicing requests; degraded means a non-critical dependency is impaired).
* ``unhealthy`` — at least one *required* probe failed or timed out. HTTP 503
  so orchestrators can route traffic away.

The per-probe outcome is reported in the ``services`` map as one of
``"ok"`` / ``"unavailable"`` / ``"timeout"``. A probe that returns any string
other than ``"ok"`` is treated as ``"unavailable"`` — probes signal failure
either by raising or by returning a non-``"ok"`` string.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

ProbeOutcome = Literal["ok", "unavailable", "timeout"]
ReadinessStatus = Literal["healthy", "degraded", "unhealthy"]

DEFAULT_TIMEOUT_SECONDS = 3.0


class ReadinessResponse(BaseModel):
    """Response model for ``GET /health/ready``.

    Mirrors the ``ReadinessResponse`` schema in
    [`wxyc-shared/api.yaml`](https://github.com/WXYC/wxyc-shared/blob/main/api.yaml).
    Conformance between the two is asserted in [WXYC/wxyc-fastapi#4](https://github.com/WXYC/wxyc-fastapi/issues/4).

    The local Pydantic model exists so FastAPI surfaces the response shape in
    OpenAPI docs at ``/docs`` and so consumers that auto-generate clients (e.g.
    semantic-index) see typed readiness data.
    """

    status: ReadinessStatus
    services: dict[str, ProbeOutcome]

    model_config = {"extra": "allow"}


@dataclass(frozen=True)
class Check:
    """A single readiness probe.

    Attributes:
        name: Surfaces in the response ``services`` map. Must be unique within
            a router; duplicates silently overwrite each other.
        probe: Async callable returning ``"ok"`` on success. Raising or
            returning any other string marks the probe as ``"unavailable"``.
        required: When ``True`` (the default), a failing probe makes the whole
            response ``unhealthy`` (HTTP 503). When ``False``, a failing probe
            downgrades the response to ``degraded`` (HTTP 200) but does not
            block the orchestrator from routing traffic.
    """

    name: str
    probe: Callable[[], Awaitable[str]]
    required: bool = True


async def _run_probe(check: Check, timeout: float) -> ProbeOutcome:
    try:
        result = await asyncio.wait_for(check.probe(), timeout=timeout)
    except TimeoutError:
        logger.warning("readiness probe %r timed out after %.3fs", check.name, timeout)
        return "timeout"
    except Exception:
        logger.exception("readiness probe %r raised", check.name)
        return "unavailable"
    if result != "ok":
        logger.warning(
            "readiness probe %r returned non-ok value %r; treated as unavailable",
            check.name,
            result,
        )
        return "unavailable"
    return "ok"


def readiness_router(checks: list[Check], *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> APIRouter:
    """Build a readiness router that probes ``checks`` concurrently.

    Args:
        checks: Probes to run. Order is preserved in the response.
        timeout: Per-probe deadline in seconds. Probes that exceed this are
            reported as ``"timeout"`` and treated as failures.

    Returns:
        A fresh :class:`fastapi.APIRouter` exposing ``GET /health/ready``.
        Calling this function again returns an independent router.
    """
    router = APIRouter()

    @router.get("/health/ready", response_model=ReadinessResponse)
    async def readiness() -> JSONResponse:
        outcomes = await asyncio.gather(*(_run_probe(c, timeout) for c in checks))
        services: dict[str, ProbeOutcome] = {
            check.name: outcome for check, outcome in zip(checks, outcomes, strict=True)
        }

        status = _aggregate_status(checks, outcomes)
        http_status = 503 if status == "unhealthy" else 200
        # JSONResponse (not the Pydantic model directly) so we can stamp HTTP
        # 503 when status == "unhealthy"; FastAPI's default response wrapping
        # would give us 200.
        return JSONResponse(
            status_code=http_status,
            content={"status": status, "services": services},
        )

    return router


def _aggregate_status(checks: list[Check], outcomes: list[ProbeOutcome]) -> ReadinessStatus:
    any_required_failed = False
    any_optional_failed = False
    for check, outcome in zip(checks, outcomes, strict=True):
        if outcome == "ok":
            continue
        if check.required:
            any_required_failed = True
        else:
            any_optional_failed = True
    if any_required_failed:
        return "unhealthy"
    if any_optional_failed:
        return "degraded"
    return "healthy"
