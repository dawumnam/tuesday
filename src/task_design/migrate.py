"""Plain-SQL migration runner.

Files ``migrations/NNNN_name.sql`` are applied in numeric order, each in its own
transaction, and recorded in ``schema_migrations``. Already-applied versions are
skipped. Run with ``python -m task_design.migrate`` (uses ``TD_DATABASE_URL``).
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg.rows import dict_row, tuple_row

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILENAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in directory.glob("*.sql"):
        match = _FILENAME.match(path.name)
        if not match:
            raise ValueError(f"migration file name must be NNNN_name.sql: {path.name}")
        found.append((int(match.group(1)), path))
    found.sort()
    versions = [v for v, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration numbers in {directory}")
    return found


def apply_migrations(
    url: str, directory: Path = MIGRATIONS_DIR, *, checkpointer: bool = True
) -> list[str]:
    """Apply pending migrations; return the file names applied.

    With ``checkpointer`` (default), also creates/upgrades the LangGraph
    Postgres checkpoint tables (``PostgresSaver.setup``) under the same lock.
    """
    applied: list[str] = []
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    integer PRIMARY KEY,
                name       text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        # Serialise concurrent runners against the same database.
        conn.execute("SELECT pg_advisory_lock(hashtext('task_design.schema_migrations'))")
        try:
            done = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for version, path in discover(directory):
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                        (version, path.name),
                    )
                applied.append(path.name)
            if checkpointer:
                _setup_checkpointer(conn)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('task_design.schema_migrations'))")
    return applied


def _setup_checkpointer(conn: psycopg.Connection) -> None:
    from langgraph.checkpoint.postgres import PostgresSaver

    conn.row_factory = dict_row  # PostgresSaver reads rows by column name
    try:
        PostgresSaver(conn).setup()  # type: ignore[arg-type]
    finally:
        conn.row_factory = tuple_row


def main() -> None:
    from task_design.settings import Settings

    for name in apply_migrations(Settings().database_url):
        print(f"applied {name}")


if __name__ == "__main__":
    main()
