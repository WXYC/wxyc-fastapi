"""Tests for ``wxyc_fastapi.db.lazy_pg.LazyPgConnection``.

Lifted from semantic-index ``utils.py`` (the original implementation has no
in-repo unit tests; these pin the contract for the extracted module). The
module is sync-psycopg by design — semantic-index runs the wrapper inside a
single-threaded ETL pipeline where the failure mode is "DSN missing or
unreachable", not concurrent first-callers.
"""

from __future__ import annotations

import pytest

pytest.importorskip("psycopg")

import psycopg  # noqa: E402

from wxyc_fastapi.db.lazy_pg import LazyPgConnection  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeConn:
    """Stand-in for ``psycopg.Connection`` that records open/close transitions."""

    def __init__(self) -> None:
        self.closed: bool = False

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# DSN absent → ``get()`` returns ``None`` without touching psycopg.connect
# ---------------------------------------------------------------------------


class TestNoDsn:
    def test_get_returns_none_when_dsn_is_none(self, monkeypatch):
        """``LazyPgConnection(None, ...)`` is the explicit "feature off" state.

        Pipeline modules instantiate the wrapper unconditionally and rely on
        ``get()`` returning ``None`` to skip the PG-backed code path; calling
        ``psycopg.connect`` here would defeat the graceful-degradation
        contract.
        """
        called = False

        def _fail(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("psycopg.connect must not be called when dsn is None")

        monkeypatch.setattr(psycopg, "connect", _fail)

        conn = LazyPgConnection(dsn=None, label="discogs-cache")

        assert conn.get() is None
        assert conn.get() is None
        assert called is False


# ---------------------------------------------------------------------------
# Lazy connect on first ``get()`` + identity caching across subsequent calls
# ---------------------------------------------------------------------------


class TestLazyConnect:
    def test_does_not_connect_at_construction(self, monkeypatch):
        """Constructor must not open the connection — first ``get()`` does."""
        called = False

        def _fail(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("psycopg.connect must not be called at construction")

        monkeypatch.setattr(psycopg, "connect", _fail)

        LazyPgConnection(dsn="postgresql://x", label="discogs-cache")

        assert called is False

    def test_first_get_opens_connection_with_dsn_and_autocommit(self, monkeypatch):
        captured: dict = {}
        fake = _FakeConn()

        def _connect(dsn, *, autocommit):
            captured["dsn"] = dsn
            captured["autocommit"] = autocommit
            return fake

        monkeypatch.setattr(psycopg, "connect", _connect)

        conn = LazyPgConnection(dsn="postgresql://example", label="discogs-cache")
        result = conn.get()

        assert result is fake
        assert captured == {"dsn": "postgresql://example", "autocommit": True}

    def test_subsequent_get_returns_cached_connection(self, monkeypatch):
        """Connection is opened at most once while it stays open."""
        invocations = 0

        def _connect(dsn, *, autocommit):
            nonlocal invocations
            invocations += 1
            return _FakeConn()

        monkeypatch.setattr(psycopg, "connect", _connect)

        conn = LazyPgConnection(dsn="postgresql://x", label="discogs-cache")
        first = conn.get()
        second = conn.get()
        third = conn.get()

        assert first is second is third
        assert invocations == 1


# ---------------------------------------------------------------------------
# Reconnect after the underlying connection is closed
# ---------------------------------------------------------------------------


class TestReconnect:
    def test_reconnects_after_underlying_connection_closes(self, monkeypatch):
        """A closed connection must trigger a fresh ``psycopg.connect`` next call."""
        connections: list[_FakeConn] = []

        def _connect(dsn, *, autocommit):
            new = _FakeConn()
            connections.append(new)
            return new

        monkeypatch.setattr(psycopg, "connect", _connect)

        conn = LazyPgConnection(dsn="postgresql://x", label="discogs-cache")

        first = conn.get()
        assert first is connections[0]

        first.close()  # simulate server-side disconnect / pool eviction

        second = conn.get()
        assert second is not first
        assert second is connections[1]
        assert len(connections) == 2


# ---------------------------------------------------------------------------
# Connection failure → log + return None, retry on next call
# ---------------------------------------------------------------------------


class TestConnectFailure:
    def test_returns_none_when_connect_raises(self, monkeypatch, caplog):
        """``psycopg.connect`` failures degrade gracefully (return ``None``).

        The wrapper logs at WARNING with ``exc_info`` so operators see the
        underlying ``OperationalError`` without the pipeline aborting — the
        consumer code branches on the ``None`` return.
        """

        def _connect(dsn, *, autocommit):
            raise psycopg.OperationalError("connection refused")

        monkeypatch.setattr(psycopg, "connect", _connect)

        conn = LazyPgConnection(dsn="postgresql://unreachable", label="discogs-cache")

        with caplog.at_level("WARNING", logger="wxyc_fastapi.db.lazy_pg"):
            result = conn.get()

        assert result is None
        assert any("discogs-cache" in r.getMessage() for r in caplog.records)

    def test_retries_on_subsequent_get_after_failure(self, monkeypatch):
        """A transient failure must not permanently wedge the wrapper."""
        attempts = 0
        fake = _FakeConn()

        def _connect(dsn, *, autocommit):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise psycopg.OperationalError("transient")
            return fake

        monkeypatch.setattr(psycopg, "connect", _connect)

        conn = LazyPgConnection(dsn="postgresql://x", label="discogs-cache")

        assert conn.get() is None  # first attempt fails
        assert conn.get() is fake  # second attempt succeeds
        assert attempts == 2


# ---------------------------------------------------------------------------
# Independence — two wrappers maintain separate state
# ---------------------------------------------------------------------------


class TestIndependence:
    def test_two_wrappers_do_not_share_connections(self, monkeypatch):
        """Each ``LazyPgConnection`` instance owns its own underlying connection."""
        captured: list[str] = []

        def _connect(dsn, *, autocommit):
            captured.append(dsn)
            return _FakeConn()

        monkeypatch.setattr(psycopg, "connect", _connect)

        a = LazyPgConnection(dsn="postgresql://a", label="discogs-cache")
        b = LazyPgConnection(dsn="postgresql://b", label="musicbrainz-cache")

        conn_a = a.get()
        conn_b = b.get()

        assert conn_a is not conn_b
        assert captured == ["postgresql://a", "postgresql://b"]
