# Healthcheck

`liveness_router` exposes `GET /health` and runs no probes — it is a process-up signal for orchestrators (Railway, Docker `HEALTHCHECK`, ECS task health). `readiness_router(checks, *, timeout=3.0)` is a *factory* that returns a fresh `APIRouter` exposing `GET /health/ready`; calling it again returns an independent router.

Each `Check(name, probe, required=True)` is an async probe. Probes return `"ok"` on success. Raising or returning any other string is treated as `"unavailable"`; exceeding `timeout` is reported as `"timeout"`. Aggregation: any required failure → `unhealthy` (HTTP 503); any optional failure → `degraded` (HTTP 200); otherwise `healthy`. `Check.required` defaults to `True` because forgetting to mark a probe required would silently downgrade an unhealthy service to `degraded`.

The response shape conforms to `HealthCheckResponse` / `ReadinessResponse` in `wxyc-shared/api.yaml` (Phase C). The conformance is asserted by `tests/healthcheck/test_conformance.py` — see "Conformance test" below.

## Conformance test

`tests/healthcheck/test_conformance.py` ([WXYC/wxyc-fastapi#4](https://github.com/WXYC/wxyc-fastapi/issues/4)) is the cross-language contract pin: a breaking change to the local Pydantic `ReadinessResponse` model **or** to the `HealthCheckResponse` / `ReadinessResponse` schemas in [`wxyc-shared/api.yaml`](https://github.com/WXYC/wxyc-shared/blob/main/api.yaml) fails this test in CI.

The test reads from a vendored snapshot at `tests/healthcheck/fixtures/api-yaml-schemas.json` (currently pinned to `wxyc-shared@v0.13.0`) so it runs hermetically — no network at test time. The snapshot stores the raw OpenAPI component schemas plus a `_pinned_version` marker; the test re-asserts the pin, so a stale fixture cannot drift silently.

The comparison is *focused*, not whole-schema: OpenAPI 3 and Pydantic's `model_json_schema()` differ syntactically (titles, `allOf` vs flattened `properties`, `$ref` resolution) in ways that are noise, not contract. The asserted invariants are: the `status` enum members (`healthy`/`degraded`/`unhealthy`), the per-service value enum (`ok`/`unavailable`/`timeout`), the required-fields surface (`status` + `services`), and `additionalProperties: true` on the openable parent. Three round-trip cases (healthy / degraded / unhealthy) validate the same payload through both the api.yaml schema (via [`jsonschema`](https://python-jsonschema.readthedocs.io/) + [`referencing`](https://referencing.readthedocs.io/)) and the local Pydantic model so the test fails if either side accepts what the other rejects.

### Refresh the fixture

When `wxyc-shared` cuts a new release that touches either schema:

```bash
python scripts/sync-api-yaml-schemas.py --ref vX.Y.Z
```

This rewrites `tests/healthcheck/fixtures/api-yaml-schemas.json` from `https://raw.githubusercontent.com/WXYC/wxyc-shared/<ref>/api.yaml`. Then update `_PINNED_REF` in `test_conformance.py` to match (the test re-asserts the pin) and commit both together. If the refresh exposes a real conformance gap (the test starts failing on the new fixture), decide deliberately whether to update the local Pydantic model or push back on the wxyc-shared change — the failure mode is the test catching a contract drift, not a bug in the test.

The script and the conformance test depend on `jsonschema>=4.21` and `pyyaml>=6.0` from the `[dev]` extra.
