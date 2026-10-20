"""Throw-away, migrated test databases on a shared Postgres server.

Each one gets a unique name, so several worktrees (or test modules) can use one
server at the same time.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from task_design.migrate import apply_migrations

DEFAULT_TEST_DATABASE_URL = "postgresql://task_design:task_design@localhost:54329/task_design"


def admin_url() -> str:
    """Server to create test databases on: ``TEST_DATABASE_URL`` or the compose instance."""
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


@contextmanager
def fresh_database(prefix: str = "td_test") -> Iterator[str]:
    """Create a uniquely named database, apply every migration, yield its URL, drop it."""
    admin = admin_url()
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(admin)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        apply_migrations(url)
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
