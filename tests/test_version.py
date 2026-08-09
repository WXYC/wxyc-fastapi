"""Pin __version__ to pyproject.toml so release-version drift cannot recur.

The 1.3.0 cut shipped with __init__.py still claiming 1.2.0 — anything trusting
``wxyc_fastapi.__version__`` misidentified the deployed version. This test makes
the next desync a red suite instead of a silent drift.
"""

import pathlib
import tomllib

import wxyc_fastapi


def test_version_matches_pyproject():
    pyproject = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert wxyc_fastapi.__version__ == data["project"]["version"]
