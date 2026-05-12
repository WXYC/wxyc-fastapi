#!/usr/bin/env python3
"""Refresh tests/healthcheck/fixtures/api-yaml-schemas.json from a wxyc-shared tag.

Usage:
    python scripts/sync-api-yaml-schemas.py [--ref vX.Y.Z]

The fixture vendors the `HealthCheckResponse` and `ReadinessResponse` component
schemas from `wxyc-shared/api.yaml` at a pinned tag so the conformance test in
`tests/healthcheck/test_conformance.py` runs hermetically — no network at test
time, and the comparison is reproducible across machines.

When the wxyc-shared schema changes, run this script with the new tag, commit
the fixture diff alongside any code changes needed to keep the local Pydantic
model in conformance, and update `_PINNED_REF` in the conformance test (it is
re-asserted there so a stale fixture cannot drift silently).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import yaml

DEFAULT_REF = "v0.13.0"
RAW_URL = "https://raw.githubusercontent.com/WXYC/wxyc-shared/{ref}/api.yaml"
FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "healthcheck"
    / "fixtures"
    / "api-yaml-schemas.json"
)
SCHEMAS_TO_VENDOR = ("HealthCheckResponse", "ReadinessResponse")


def fetch_api_yaml(ref: str) -> dict:
    url = RAW_URL.format(ref=ref)
    with urllib.request.urlopen(url) as resp:  # noqa: S310 — github raw, fixed scheme
        return yaml.safe_load(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ref",
        default=DEFAULT_REF,
        help=f"wxyc-shared git ref to vendor (default: {DEFAULT_REF}).",
    )
    args = parser.parse_args()

    spec = fetch_api_yaml(args.ref)
    schemas = spec["components"]["schemas"]
    missing = [name for name in SCHEMAS_TO_VENDOR if name not in schemas]
    if missing:
        print(f"error: schemas not found in api.yaml@{args.ref}: {missing}", file=sys.stderr)
        return 1

    fixture = {
        "_source": RAW_URL.format(ref=args.ref),
        "_pinned_version": args.ref,
    }
    for name in SCHEMAS_TO_VENDOR:
        fixture[name] = schemas[name]

    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FIXTURE_PATH.open("w", encoding="utf-8") as f:
        json.dump(fixture, f, indent=2, sort_keys=False, ensure_ascii=False)
        f.write("\n")
    print(f"wrote {FIXTURE_PATH} from wxyc-shared@{args.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
