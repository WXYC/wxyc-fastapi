"""Database helpers for WXYC FastAPI services.

The single export today is :class:`LazyPgConnection` — a lazy, reconnecting
sync ``psycopg`` wrapper extracted from semantic-index's ``utils.py``. Behind
the ``[psycopg]`` optional extra so consumers that don't talk to PostgreSQL
don't pull in the driver.
"""

from wxyc_fastapi.db.lazy_pg import LazyPgConnection

__all__ = ["LazyPgConnection"]
