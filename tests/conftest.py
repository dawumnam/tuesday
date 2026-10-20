"""Shared test fixtures.

Every test session gets its own freshly created, uniquely named database on the
Postgres server at ``TEST_DATABASE_URL`` (default: the compose instance), so
several worktrees can run the suite concurrently against one server.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from task_design.db import Database
from task_design.service import TaskDesignService
from task_design.settings import Settings
from task_design.testing.database import fresh_database
from task_design.testing.fake_gateway import FakeGateway
from task_design.testing.workflow import BackgroundRunner


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a fresh, migrated database that lives for the whole test session."""
    with fresh_database("td_test") as url:
        yield url


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


@pytest.fixture
def background(db: Database) -> Iterator[BackgroundRunner]:
    """A threaded ``run_graph``; its jobs are joined before ``db`` closes."""
    runner = BackgroundRunner()
    yield runner
    runner.join()
