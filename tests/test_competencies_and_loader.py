"""Loader (JSON files → tables) read back through the two repositories."""

from __future__ import annotations

import json
import uuid

import pytest

from task_design.domain import Level
from task_design.loader import load_dataset
from task_design.repositories import CompetencyRepository, TaskRepository


def _competency(cid: str, name: str) -> dict:
    return {
        "id": cid,
        "name": name,
        "description": f"{name} 설명",
        "sub_elements": [
            {
                "id": f"{cid}.1",
                "name": "출처 확인",
                "indicators": {
                    "high": "AI 답변의 출처를 스스로 교차 검증한다.",
                    "medium": "요청받으면 출처를 확인한다.",
                    "low": "AI 답변을 그대로 받아들인다.",
                },
            },
            {
                "id": f"{cid}.2",
                "name": "오류 수정",
                "indicators": {
                    "high": "발견한 오류를 근거와 함께 고친다.",
                    "medium": "명백한 오류만 고친다.",
                    "low": "오류를 알아차리지 못한다.",
                },
            },
        ],
    }


def _task(competency_id: str, scenario: str) -> dict:
    item = {
        "competency_id": competency_id,
        "criteria_zero": "없음",
        "criteria_partial": "일부",
        "criteria_full": "충분",
    }
    return {
        "id": str(uuid.uuid4()),
        "scenario": scenario,
        "instruction": "요약 보고서를 작성하라.",
        "attachments": [
            {
                "filename": "sales.csv",
                "format": "csv",
                "content": "월,매출\n1월,100\n2월,-50",
                "traps": [{"location": "3행 매출 열", "description": "음수 매출"}],
            }
        ],
        "rubric": {
            "items": [
                {"title": "검증 과정", "type": "process", "points": 40, **item},
                {"title": "정확성", "type": "output", "points": 30, **item},
                {"title": "구성", "type": "output", "points": 30, **item},
            ]
        },
        "competency_ids": [competency_id],
        "audience": {"job": "영업 관리", "career_level": "3년차"},
        "difficulty": "high",
    }


@pytest.fixture
def dataset(tmp_path):
    a, b = f"T{uuid.uuid4().hex[:8]}", f"T{uuid.uuid4().hex[:8]}"
    task = _task(a, f"월간 매출 자료의 이상값을 AI로 점검하는 상황 {uuid.uuid4().hex}")
    (tmp_path / "competencies.json").write_text(
        json.dumps([_competency(a, "AI 결과 검증"), _competency(b, "프롬프트 설계")], ensure_ascii=False)
    )
    (tmp_path / "tasks.json").write_text(json.dumps([task], ensure_ascii=False))
    return tmp_path, a, b, task


def test_loaded_competencies_are_readable(dataset, db, fake_gateway, settings):
    directory, a, b, _ = dataset
    load_dataset(directory, db, fake_gateway, settings)
    repo = CompetencyRepository(db)

    competency = repo.get(a)
    assert competency.name == "AI 결과 검증"
    assert [s.name for s in competency.sub_elements] == ["출처 확인", "오류 수정"]
    assert {a, b} <= {c.id for c in repo.list()}
    assert [i.description for i in repo.level_indicators(a, Level.LOW)] == [
        "AI 답변을 그대로 받아들인다.",
        "오류를 알아차리지 못한다.",
    ]
    assert repo.get("no-such-competency") is None


def test_loaded_tasks_are_searchable(dataset, db, fake_gateway, settings):
    directory, _, _, task = dataset
    load_dataset(directory, db, fake_gateway, settings)
    repo = TaskRepository(db, fake_gateway, settings)

    stored = repo.get(uuid.UUID(task["id"]))
    assert stored.attachments[0].traps[0].description == "음수 매출"
    assert stored.id in [t.id for t in repo.hybrid_search(task["scenario"], k=3)]


def test_loading_twice_is_idempotent(dataset, db, fake_gateway, settings):
    directory, a, _, task = dataset
    load_dataset(directory, db, fake_gateway, settings)
    load_dataset(directory, db, fake_gateway, settings)

    assert len(CompetencyRepository(db).get(a).sub_elements) == 2
    repo = TaskRepository(db, fake_gateway, settings)
    assert [m.task.id for m in repo.find_duplicates(task["scenario"], threshold=0.99)] == [
        uuid.UUID(task["id"])
    ]


def test_task_with_unknown_competency_is_rejected(tmp_path, db, fake_gateway, settings):
    (tmp_path / "competencies.json").write_text("[]")
    (tmp_path / "tasks.json").write_text(json.dumps([_task("UNKNOWN", "상황")]))
    with pytest.raises(ValueError, match="UNKNOWN"):
        load_dataset(tmp_path, db, fake_gateway, settings)
