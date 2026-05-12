"""Lazy, reconnecting sync ``psycopg`` connection wrapper.

Lifted verbatim from semantic-index's ``utils.py`` (lines 14-42). The
implementation is sync because semantic-index's pipeline is sync — it runs
inside a single-threaded ETL that opens at most one connection per cache
(Discogs, MusicBrainz, Wikidata, AcousticBrainz) and tolerates the connection
being absent. Async services that need a pool should use the
:func:`wxyc_fastapi.http.singleton.async_singleton` helper to wrap
``asyncpg.create_pool`` instead — that path has the double-check-lock
required to survive concurrent first-callers (the
[LML#241](https://github.com/WXYC/library-metadata-lookup/issues/241)
race).

Why this exists
---------------
Pipeline modules (``discogs_client``, ``musicbrainz_client``,
``wikidata_client``, ``acousticbrainz_client``, ``graph_metrics``) want a
single, late-bound PG handle that:

* opens on first ``get()`` (so import-time DSN absence isn't fatal);
* returns ``None`` when no DSN is configured (the explicit "feature off"
  state — pipeline branches skip PG-backed code paths cleanly);
* returns ``None`` on connection failure rather than raising (so a
  transient cache outage degrades the run instead of aborting it);
* transparently reconnects when the underlying connection is closed
  (server-side disconnects, pool eviction, idle timeouts).

Returning ``None`` is a load-bearing contract — callers branch on it. The
class deliberately does not raise.
"""

from __future__ import annotations

import logging

import psycopg

logger = logging.getLogger(__name__)


class LazyPgConnection:
    """Lazy, reconnecting PostgreSQL connection wrapper.

    Defers connection creation until first use and transparently reconnects
    when the connection is closed. Returns ``None`` when no DSN is configured
    or on connection failure, matching the graceful-degradation pattern used
    by the pipeline's PostgreSQL clients.

    Args:
        dsn: PostgreSQL connection string, or ``None`` to disable.
        label: Human-readable name for log messages (e.g. ``"discogs-cache"``).
    """

    def __init__(self, dsn: str | None, label: str) -> None:
        self._dsn = dsn
        self._label = label
        self._conn: psycopg.Connection | None = None

    def get(self) -> psycopg.Connection | None:
        """Return an open connection, or ``None`` if unavailable."""
        if self._dsn is None:
            return None
        if self._conn is None or self._conn.closed:
            try:
                self._conn = psycopg.connect(self._dsn, autocommit=True)
            except Exception:
                logger.warning("Failed to connect to %s", self._label, exc_info=True)
                return None
        return self._conn
