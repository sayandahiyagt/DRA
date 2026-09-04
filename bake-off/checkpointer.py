"""AsyncPostgresSaver setup for the §38.1 bake-off (non-canonical prototype).

Mirrors the proven pattern in ``src/dra/control_plane.py`` (dra#36):
``AsyncPostgresSaver.from_conn_string(postgres_conninfo(DATABASE_URL))`` then
``.setup()``. ``postgres_conninfo`` strips the SQLAlchemy ``+psycopg`` dialect
suffix so psycopg/libpq accepts the connection string (passing the
``postgresql+psycopg://`` URL raises ``psycopg.ProgrammingError: missing =``).
"""
from __future__ import annotations

from dra.db import DATABASE_URL


def postgres_conninfo(db_url: str | None = None) -> str:
    """Convert the SQLAlchemy ``postgresql+psycopg://`` URL to a libpq URL."""
    db_url = db_url or DATABASE_URL
    if db_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + db_url[len("postgresql+psycopg://"):]
    return db_url


def make_checkpointer():
    """Return (saver, checkpointer_cm) — the saver is an async context manager.

    Usage::

        from bakeoff.checkpointer import make_checkpointer
        async with make_checkpointer() as ckpt:
            await ckpt.setup()
            graph = build_graph().compile(checkpointer=ckpt, ...)
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    return AsyncPostgresSaver.from_conn_string(postgres_conninfo())
