"""`task-design seed`: the committed synthetic dataset loaded into a fresh database."""

from __future__ import annotations

import json
import os
import uuid
from collections import Counter
from collections.abc import Iterator

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from task_design.cli import DEFAULT_SEED_DIR, main
from task_design.db import Database
from task_design.domain import Level, Rubric, RubricItemType, Task
from task_design.migrate import apply_migrations
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.settings import Settings
from task_design.testing.fake_gateway import FakeGateway

ADMIN_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://task_design:task_design@localhost:54329/task_design"
)


@pytest.fixture(scope="module")
def fresh_url() -> Iterator[str]:
    """A migrated database of its own: the session DB is shared and never truncated."""
    name = f"td_seed_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    params = conninfo_to_dict(ADMIN_URL)
    params["dbname"] = name
    url = make_conninfo(**params)
    try:
        apply_migrations(url)
        yield url
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def seeded_db(fresh_url, monkeypatch) -> Iterator[Database]:
    monkeypatch.setenv("TD_DATABASE_URL", fresh_url)
    assert main(["seed", "--fake"]) == 0
    db = Database(fresh_url)
    try:
        yield db
    finally:
        db.close()


def test_seed_loads_the_competency_framework(seeded_db):
    competencies = CompetencyRepository(seeded_db).list()

    assert [c.id for c in competencies] == [f"C{i}" for i in range(1, 9)]
    assert all(len(c.sub_elements) >= 2 for c in competencies)


def _seed_tasks() -> list[Task]:
    raw = json.loads((DEFAULT_SEED_DIR / "tasks.json").read_text(encoding="utf-8"))
    return [Task.model_validate(t) for t in raw]


def _task_count(db: Database) -> int:
    with db.connection() as conn:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]


def _repo(db: Database) -> TaskRepository:
    settings = Settings(_env_file=None)
    return TaskRepository(db, FakeGateway(embedding_dim=settings.embedding_dim), settings)


def test_seed_loads_every_task_with_a_rubric_that_passes_validation(seeded_db):
    seeds = _seed_tasks()
    repo = _repo(seeded_db)

    assert 25 <= len(seeds) <= 35
    assert _task_count(seeded_db) == len(seeds)
    for seed in seeds:
        loaded = repo.get(seed.id)
        assert loaded is not None, seed.id
        Rubric.model_validate(loaded.rubric.model_dump())  # 3–5 items, >=1 process item


def test_seed_is_idempotent(seeded_db, fresh_url, monkeypatch):
    before = (_task_count(seeded_db), CompetencyRepository(seeded_db).list())

    monkeypatch.setenv("TD_DATABASE_URL", fresh_url)
    assert main(["seed", "--fake"]) == 0

    assert (_task_count(seeded_db), CompetencyRepository(seeded_db).list()) == before


def test_hybrid_search_over_seeded_tasks_returns_results(seeded_db):
    results = _repo(seeded_db).hybrid_search("매출 데이터의 합계와 이상값을 검증", k=5)

    assert 1 <= len(results) <= 5
    assert any("매출" in t.scenario + t.instruction for t in results)


# --- dataset content: what similar-task search and persona prompts rely on ---------


def test_every_seed_task_has_attachments_with_marked_traps():
    for t in _seed_tasks():
        assert t.attachments, t.scenario[:30]
        for a in t.attachments:
            assert a.traps, a.filename
            assert all(trap.location and trap.description for trap in a.traps)


def test_seed_rubrics_are_coherent_with_their_task():
    for t in _seed_tasks():
        rubric_competencies = {i.competency_id for i in t.rubric.items}
        assert rubric_competencies == set(t.competency_ids), t.scenario[:30]
        assert t.rubric.total_points == 100
        assert any(i.type is RubricItemType.OUTPUT for i in t.rubric.items)


def test_seed_tasks_spread_across_competencies_audiences_and_difficulties():
    tasks = _seed_tasks()
    per_competency = Counter(c for t in tasks for c in t.competency_ids)
    formats = {a.format for t in tasks for a in t.attachments}

    assert set(per_competency) == {f"C{i}" for i in range(1, 9)}
    assert min(per_competency.values()) >= 3
    assert {t.difficulty for t in tasks} == set(Level)
    assert len({t.audience.job for t in tasks}) >= 8
    assert len({t.audience.career_level for t in tasks}) >= 3
    assert len(formats) == 4
    assert len({t.id for t in tasks}) == len(tasks)
