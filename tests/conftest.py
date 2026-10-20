"""Shared test fixtures.

Every test session gets its own freshly created, uniquely named database on the
Postgres server at ``TEST_DATABASE_URL`` (default: the compose instance), so
several worktrees can run the suite concurrently against one server.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from task_design.db import Database
from task_design.migrate import apply_migrations
from task_design.service import TaskDesignService
from task_design.settings import Settings
from task_design.testing.fake_gateway import FakeGateway

DEFAULT_TEST_DATABASE_URL = "postgresql://task_design:task_design@localhost:54329/task_design"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a fresh, migrated database that lives for the whole test session."""
    admin_url = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    name = f"td_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(admin_url)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        apply_migrations(url)
        yield url
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def settings(database_url: str) -> Settings:
    # _env_file=None: tests never read a developer's .env.
    return Settings(database_url=database_url, _env_file=None)


@pytest.fixture
def db(settings: Settings) -> Iterator[Database]:
    database = Database(settings.database_url)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def empty_repositories(db: Database) -> Database:
    """``db`` with the 과제 DB and 역량 체계 tables emptied before the test.

    Use it when a test's results depend on everything stored (search rankings,
    listings), so rows written by earlier tests in the session can't interfere.
    """
    with db.connection() as conn:
        conn.execute("TRUNCATE tasks, competencies CASCADE")
    return db


@pytest.fixture
def fake_gateway(settings: Settings) -> FakeGateway:
    return FakeGateway(embedding_dim=settings.embedding_dim)


@pytest.fixture
def service(db: Database, fake_gateway: FakeGateway, settings: Settings) -> TaskDesignService:
    return TaskDesignService(db=db, gateway=fake_gateway, settings=settings)
