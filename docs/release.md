# Release

Tag-pushed: `git tag v0.1.0 && git push origin v0.1.0` triggers `.github/workflows/publish.yml` which builds + uploads via PyPI Trusted Publishing (no token in repo). The tag is cut after PR 3 merges, not PR 1 or PR 2.

The PyPI Trusted Publisher relationship must be configured once in the PyPI UI before the first tag push (project name `wxyc-fastapi`, owner `WXYC`, repo `wxyc-fastapi`, workflow `publish.yml`, environment `pypi`).

CHANGELOG.md must be updated for every PR. The "Consumer impact" subsection is non-optional when the change affects a consumer's observable behavior (e.g., the Sentry removal in 2.0.0).
