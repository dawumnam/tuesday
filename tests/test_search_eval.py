"""`task-design eval-search`: 유사 과제 검색 적중률 against a query set with expected tasks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import uuid4

import pytest

from task_design.cli import DEFAULT_SEED_DIR, EXAMPLE_SEARCH_EVAL_FILE, main
from task_design.db import Database
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.search_eval import STEPS, EvalQuery, evaluate, load_queries
from task_design.settings import Settings
from task_design.testing.database import fresh_database
from task_design.testing.fake_gateway import FakeGateway, ngram_embedding


@pytest.fixture(scope="module")
def seeded_url() -> Iterator[str]:
    """A database of its own holding exactly the seed dataset (demo embeddings)."""
    with fresh_database("td_search_eval") as url:
        assert main(["seed", "--fake"], settings=Settings(database_url=url, _env_file=None)) == 0
        yield url


@pytest.fixture
def seeded(seeded_url) -> Settings:
    return Settings(database_url=seeded_url, _env_file=None)


@pytest.fixture
def repositories(seeded) -> Iterator[tuple[TaskRepository, CompetencyRepository]]:
    db = Database(seeded.database_url)
    gateway = FakeGateway(embedding_dim=seeded.embedding_dim, embedder=ngram_embedding)
    try:
        yield TaskRepository(db, gateway, seeded), CompetencyRepository(db)
    finally:
        db.close()


def seed_tasks() -> list[dict]:
    return json.loads((DEFAULT_SEED_DIR / "tasks.json").read_text(encoding="utf-8"))


def query(job: str, competency: str, expected: list[str]) -> EvalQuery:
    return EvalQuery.model_validate({
        "input": {"competency_ids": [competency],
                  "audience": {"job": job, "career_level": "3년차"}, "difficulty": "medium"},
        "expected": expected,
    })


def test_a_query_hits_when_an_expected_task_is_in_the_top_k(repositories):
    tasks, competencies = repositories
    finance = next(t for t in seed_tasks() if t["audience"]["job"] == "재무·회계"
                   and "C4" in t["competency_ids"])
    marketing = next(t for t in seed_tasks() if t["audience"]["job"] == "마케팅")

    result = evaluate(tasks, competencies, [
        query("재무·회계", "C4", [finance["id"]]),
        query("재무·회계", "C4", [marketing["id"]]),  # the wrong answer for this query
    ], k=3)

    right, wrong = result.results
    assert right.hits["final"] and right.ranks["final"] <= 3
    assert right.ranks["keyword"] is not None  # the 직무 is a keyword term
    assert not wrong.hits["final"]
    assert result.hit_count("final") == 1
    assert result.hit_rate("final") == pytest.approx(0.5)
    assert set(right.ranks) == set(STEPS)


def test_the_example_query_set_names_seed_tasks_only():
    seed_ids = {t["id"] for t in seed_tasks()}

    queries = load_queries(EXAMPLE_SEARCH_EVAL_FILE)

    assert len(queries) >= 10
    assert {str(i) for q in queries for i in q.expected} <= seed_ids


def test_command_prints_every_query_and_the_hit_rate_per_step(capsys, seeded):
    code = main(["eval-search", str(EXAMPLE_SEARCH_EVAL_FILE), "--fake"], settings=seeded)

    out = capsys.readouterr().out
    total = len(load_queries(EXAMPLE_SEARCH_EVAL_FILE))
    assert code == 0
    assert f"질의 {total}개, 상위 5개 기준" in out
    assert out.count(f"/{total}] ") == total
    summary = out.strip().splitlines()[-1]
    assert summary.startswith("적중률")
    assert all(ko in summary for ko in STEPS.values())


def test_command_fails_below_the_minimum_hit_rate(tmp_path, capsys, seeded):
    marketing = next(t for t in seed_tasks() if t["audience"]["job"] == "마케팅")
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([
        query("재무·회계", "C4", [marketing["id"]]).model_dump(mode="json"),
    ], ensure_ascii=False), encoding="utf-8")

    code = main(["eval-search", str(path), "--fake", "-k", "1", "--min-hit-rate", "0.8"],
                settings=seeded)

    assert code == 1
    assert "못 미칩니다" in capsys.readouterr().err


def test_command_stops_on_an_expected_task_that_is_not_in_the_task_db(tmp_path, capsys, seeded):
    missing = str(uuid4())
    path = tmp_path / "queries.json"
    path.write_text(json.dumps([
        query("재무·회계", "C4", [missing]).model_dump(mode="json"),
    ], ensure_ascii=False), encoding="utf-8")

    code = main(["eval-search", str(path), "--fake"], settings=seeded)

    assert code == 2
    assert missing in capsys.readouterr().err


def test_command_reports_an_unreadable_query_file(tmp_path, capsys, seeded):
    path = tmp_path / "queries.json"
    path.write_text('[{"input": {}}]', encoding="utf-8")

    code = main(["eval-search", str(path), "--fake"], settings=seeded)

    assert code == 2
    assert "질의 파일을 읽을 수 없습니다" in capsys.readouterr().err
