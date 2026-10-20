"""Postgres access: a small wrapper around a psycopg 3 connection pool."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    """Owns the connection pool. Pass this around as ``db``.

    ``connection()`` yields a pooled connection inside a transaction that is
    committed on clean exit and rolled back on exception. ``url`` is exposed for
    components that manage their own connections (e.g. the LangGraph
    Postgres checkpointer).
    """

    def __init__(self, url: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.url = url
        self._min_size = min_size
        self._max_size = max_size
        self._pool: ConnectionPool | None = None
        self._autocommit_pool: ConnectionPool | None = None

    @property
    def pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                self.url, min_size=self._min_size, max_size=self._max_size, open=True
            )
        return self._pool

    @property
    def autocommit_pool(self) -> ConnectionPool:
        """Autocommit, dict-row pool in the shape ``PostgresSaver`` (LangGraph) expects."""
        if self._autocommit_pool is None:
            self._autocommit_pool = ConnectionPool(
                self.url,
                min_size=0,
                max_size=self._max_size,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=True,
            )
        return self._autocommit_pool

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        with self.pool.connection() as conn:
            yield conn

    def close(self) -> None:
        for pool in (self._pool, self._autocommit_pool):
            if pool is not None:
                pool.close()
        self._pool = self._autocommit_pool = None
