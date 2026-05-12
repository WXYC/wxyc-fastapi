"""Conformance test: local ``ReadinessResponse`` vs `wxyc-shared/api.yaml`.

Why this test exists
--------------------

The local Pydantic ``ReadinessResponse`` in
:mod:`wxyc_fastapi.healthcheck.readiness` is the Python end of a cross-language
contract whose canonical schema lives in
`wxyc-shared/api.yaml <https://github.com/WXYC/wxyc-shared/blob/main/api.yaml>`_
(component ``ReadinessResponse`` plus its ``allOf`` parent
``HealthCheckResponse``). Backend-Service consumes the same schema via
generated TypeScript types.

A breaking change to either side — adding a required field, narrowing the
``status`` enum, removing ``services``, etc. — must fail CI here. Tracked in
`WXYC/wxyc-fastapi#4 <https://github.com/WXYC/wxyc-fastapi/issues/4>`_.

How it works
------------

1. ``tests/healthcheck/fixtures/api-yaml-schemas.json`` vendors the relevant
   schemas from a pinned wxyc-shared tag (currently ``v0.13.0``). No network
   at test time. Refresh with ``python scripts/sync-api-yaml-schemas.py
   --ref vX.Y.Z``.
2. We assert load-bearing properties of the api.yaml schema (``status`` enum
   members, ``services`` value enum, ``additionalProperties`` open) against
   the local Pydantic JSON Schema. We do **not** compare full schema dicts —
   OpenAPI 3 and Pydantic's JSON Schema differ syntactically (titles,
   ``allOf`` vs flattened ``properties``, ``$ref`` resolution) in ways that
   are noise, not contract.
3. Three round-trip tests construct a healthy / degraded / unhealthy response
   dict, validate it via the local Pydantic model, and assert the
   re-serialized dict round-trips losslessly. They also validate the same
   dict against the api.yaml schema via :mod:`jsonschema` so the test fails
   if either side accepts what the other rejects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from wxyc_fastapi.healthcheck.readiness import ReadinessResponse

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "api-yaml-schemas.json"
_PINNED_REF = "v0.13.0"


@pytest.fixture(scope="module")
def api_yaml_schemas() -> dict:
    with _FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def pydantic_schema() -> dict:
    return ReadinessResponse.model_json_schema()


@pytest.fixture(scope="module")
def readiness_validator(api_yaml_schemas: dict) -> Draft202012Validator:
    """JSON Schema validator that resolves the ``allOf`` $ref to HealthCheckResponse.

    The fixture stores both component schemas at the JSON root, but
    ``ReadinessResponse.allOf[0].$ref`` points at
    ``#/components/schemas/HealthCheckResponse`` (an OpenAPI-style pointer).
    We mount a synthetic document with that exact internal layout into the
    `referencing` registry so the resolver can follow the pointer without
    needing the full api.yaml loaded.
    """
    base_uri = "https://wxyc.example/api.yaml"
    api_doc = {
        "components": {
            "schemas": {
                "HealthCheckResponse": api_yaml_schemas["HealthCheckResponse"],
                "ReadinessResponse": api_yaml_schemas["ReadinessResponse"],
            }
        }
    }
    registry = Registry().with_resource(
        uri=base_uri,
        resource=Resource(contents=api_doc, specification=DRAFT202012),
    )
    return Draft202012Validator(
        {"$ref": f"{base_uri}#/components/schemas/ReadinessResponse"},
        registry=registry,
    )


class TestFixturePin:
    def test_fixture_records_pinned_wxyc_shared_version(self, api_yaml_schemas: dict):
        # Re-asserting the pin in the test (not just the script default) ensures
        # a stale fixture cannot drift silently when the sync script default bumps.
        assert api_yaml_schemas["_pinned_version"] == _PINNED_REF
        assert _PINNED_REF in api_yaml_schemas["_source"]

    def test_fixture_contains_both_required_schemas(self, api_yaml_schemas: dict):
        assert "HealthCheckResponse" in api_yaml_schemas
        assert "ReadinessResponse" in api_yaml_schemas


class TestStatusEnum:
    """Both sides agree on the ``status`` enum."""

    EXPECTED = {"healthy", "degraded", "unhealthy"}

    def test_api_yaml_status_enum(self, api_yaml_schemas: dict):
        enum = api_yaml_schemas["HealthCheckResponse"]["properties"]["status"]["enum"]
        assert set(enum) == self.EXPECTED

    def test_pydantic_status_enum(self, pydantic_schema: dict):
        enum = pydantic_schema["properties"]["status"]["enum"]
        assert set(enum) == self.EXPECTED


class TestServicesValueEnum:
    """Both sides agree on the per-service status value enum."""

    EXPECTED = {"ok", "unavailable", "timeout"}

    def test_api_yaml_services_value_enum(self, api_yaml_schemas: dict):
        # ReadinessResponse.allOf[1].properties.services.additionalProperties.enum
        services_block = api_yaml_schemas["ReadinessResponse"]["allOf"][1]["properties"]["services"]
        enum = services_block["additionalProperties"]["enum"]
        assert set(enum) == self.EXPECTED
        assert services_block["additionalProperties"]["type"] == "string"

    def test_pydantic_services_value_enum(self, pydantic_schema: dict):
        services = pydantic_schema["properties"]["services"]
        assert services["type"] == "object"
        enum = services["additionalProperties"]["enum"]
        assert set(enum) == self.EXPECTED
        assert services["additionalProperties"]["type"] == "string"


class TestRequiredFields:
    def test_api_yaml_requires_status_and_services(self, api_yaml_schemas: dict):
        # Required surface = HealthCheckResponse.required ∪ ReadinessResponse.allOf[1].required.
        health_required = set(api_yaml_schemas["HealthCheckResponse"].get("required", []))
        readiness_required = set(
            api_yaml_schemas["ReadinessResponse"]["allOf"][1].get("required", [])
        )
        assert health_required == {"status"}
        assert readiness_required == {"services"}

    def test_pydantic_requires_status_and_services(self, pydantic_schema: dict):
        assert set(pydantic_schema["required"]) == {"status", "services"}


class TestExtraFieldsAreOpen:
    """``HealthCheckResponse`` is intentionally open (``additionalProperties: true``)
    so services like semantic-index can attach extra fields (e.g. ``artist_count``)
    without breaking consumers. The local Pydantic model must mirror this with
    ``extra: allow``.
    """

    def test_api_yaml_health_check_response_is_open(self, api_yaml_schemas: dict):
        assert api_yaml_schemas["HealthCheckResponse"]["additionalProperties"] is True

    def test_pydantic_model_allows_extras(self, pydantic_schema: dict):
        # Pydantic emits ``additionalProperties: true`` when ``model_config["extra"] == "allow"``.
        assert pydantic_schema.get("additionalProperties") is True

    def test_extras_round_trip_through_pydantic_model(self):
        payload = {
            "status": "healthy",
            "services": {"db": "ok"},
            "artist_count": 12345,  # semantic-index style extension
        }
        validated = ReadinessResponse.model_validate(payload)
        # Pydantic v2 stores extras in ``__pydantic_extra__``; ``model_dump`` includes them.
        assert validated.model_dump() == payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {"status": "healthy", "services": {"db": "ok", "cache": "ok"}},
            id="healthy",
        ),
        pytest.param(
            {"status": "degraded", "services": {"db": "ok", "discogs": "unavailable"}},
            id="degraded-optional-failure",
        ),
        pytest.param(
            {"status": "unhealthy", "services": {"db": "timeout", "cache": "ok"}},
            id="unhealthy-required-timeout",
        ),
    ],
)
class TestResponseRoundTrip:
    """A response dict valid under api.yaml round-trips through the Pydantic model
    losslessly, and vice versa.
    """

    def test_payload_validates_against_api_yaml_schema(
        self, payload: dict, readiness_validator: Draft202012Validator
    ):
        readiness_validator.validate(payload)

    def test_payload_validates_against_pydantic_model(self, payload: dict):
        ReadinessResponse.model_validate(payload)

    def test_pydantic_round_trip_preserves_payload(self, payload: dict):
        validated = ReadinessResponse.model_validate(payload)
        assert validated.model_dump() == payload


class TestRejectionParity:
    """Both sides reject payloads that violate the contract."""

    def test_api_yaml_rejects_unknown_status(self, readiness_validator: Draft202012Validator):
        from jsonschema import ValidationError

        with pytest.raises(ValidationError):
            readiness_validator.validate({"status": "weird", "services": {}})

    def test_pydantic_rejects_unknown_status(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReadinessResponse.model_validate({"status": "weird", "services": {}})

    def test_api_yaml_rejects_unknown_service_outcome(
        self, readiness_validator: Draft202012Validator
    ):
        from jsonschema import ValidationError

        with pytest.raises(ValidationError):
            readiness_validator.validate({"status": "healthy", "services": {"db": "kinda-ok"}})

    def test_pydantic_rejects_unknown_service_outcome(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReadinessResponse.model_validate({"status": "healthy", "services": {"db": "kinda-ok"}})

    def test_api_yaml_requires_status_field(self, readiness_validator: Draft202012Validator):
        from jsonschema import ValidationError

        with pytest.raises(ValidationError):
            readiness_validator.validate({"services": {}})

    def test_pydantic_requires_status_field(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ReadinessResponse.model_validate({"services": {}})
