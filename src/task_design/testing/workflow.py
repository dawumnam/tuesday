"""Test helpers for driving the workflow through ``TaskDesignService``.

Typical arrange step for "a draft in 상황 선택 대기"::

    draft = start_awaiting_selection(service, fake_gateway, db, settings)

``ScenarioWriter`` answers 상황 후보 generation calls (schema ``CandidateBatch``)
with fresh, mutually distinct scenarios that reference the 유사 과제 shown in
the prompt. Combine it with responders for other GENERATION schemas via
``by_schema``::

    fake.respond_with(Role.GENERATION, by_schema({
        CandidateBatch: ScenarioWriter(),
        SomeInstructionSchema: lambda call: {...},
    }))
"""

from __future__ import annotations

import itertools
import re
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from task_design.db import Database
from task_design.domain import (
    Audience,
    Draft,
    DraftInput,
    Level,
    Rubric,
    RubricItem,
    RubricItemType,
    Task,
)
from task_design.gateway import ModelGateway, Role
from task_design.repositories import TaskRepository
from task_design.settings import Settings
from task_design.testing.fake_gateway import FakeCall, FakeGateway
from task_design.workflow.schemas import CandidateBatch

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def make_input(**overrides: Any) -> DraftInput:
    fields: dict[str, Any] = {
        "competency_ids": ["C1"],
        "audience": Audience(job="마케팅", career_level="3년차"),
        "difficulty": Level.MEDIUM,
        "memo": None,
    }
    fields.update(overrides)
    return DraftInput(**fields)


def make_task(scenario: str, instruction: str = "보고서를 작성하라.") -> Task:
    item = {"competency_id": "C1", "criteria_zero": "없음", "criteria_partial": "일부", "criteria_full": "충분"}
    return Task(
        scenario=scenario,
        instruction=instruction,
        rubric=Rubric(
            items=[
                RubricItem(title="검증", type=RubricItemType.PROCESS, points=30, **item),
                RubricItem(title="정확성", type=RubricItemType.OUTPUT, points=40, **item),
                RubricItem(title="구성", type=RubricItemType.OUTPUT, points=30, **item),
            ]
        ),
        competency_ids=["C1"],
        audience=Audience(job="마케팅", career_level="3년차"),
        difficulty=Level.MEDIUM,
    )


TEST_COMPETENCY_IDS = ("C1", "C2")


def ensure_competencies(db: Database, ids: tuple[str, ...] = TEST_COMPETENCY_IDS) -> None:
    """Make sure the 역량 체계 has these competencies (``start`` rejects unknown ids).

    Inserts a small competency with 2 sub-elements and 상·중·하 수준 지표 for each
    missing id; existing rows (e.g. the seed data) are left untouched. Safe after
    ``empty_repositories`` truncated the tables.
    """
    with db.connection() as conn:
        for cid in ids:
            conn.execute(
                "INSERT INTO competencies (id, name, description) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (cid, f"테스트 역량 {cid}", "테스트용 역량"),
            )
            for pos in (1, 2):
                sid = f"{cid}.{pos}"
                conn.execute(
                    "INSERT INTO competency_sub_elements (id, competency_id, position, name, description) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (sid, cid, pos, f"하위 요소 {sid}", ""),
                )
                for level in Level:
                    conn.execute(
                        "INSERT INTO level_indicators (sub_element_id, level, description) "
                        "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (sid, level.value, f"{sid} {level.ko} 수준 행동"),
                    )


def seed_similar_tasks(
    db: Database, gateway: ModelGateway, settings: Settings, n: int = 3
) -> list[uuid.UUID]:
    """Add ``n`` fresh tasks to the task DB so 유사 과제 검색 has something to find.

    Also ensures the test competencies exist (``ensure_competencies``).
    """
    ensure_competencies(db)
    repo = TaskRepository(db, gateway, settings)
    tag = uuid.uuid4().hex[:8]
    return [repo.add(make_task(f"[{tag}] 기존 과제 상황 {i}"), source="seed") for i in range(n)]


def referenced_ids(call: FakeCall) -> list[str]:
    """Task ids that appear in the call's prompt (the 유사 과제 shown to the model)."""
    return list(dict.fromkeys(_UUID.findall(call.prompt)))


def candidate(scenario: str, title: str | None = None, refs: list[str] | None = None) -> dict:
    return {"title": title or scenario[:20], "scenario": scenario, "referenced_task_ids": refs or []}


class ScenarioWriter:
    """Responder for 상황 후보 generation: 3 new distinct scenarios per call.

    Each candidate references every 유사 과제 id visible in the prompt.
    ``scenarios`` lists every scenario text it has produced, in order.
    """

    def __init__(self, prefix: str | None = None) -> None:
        self.prefix = prefix or f"상황-{uuid.uuid4().hex[:8]}"
        self._n = itertools.count(1)
        self.scenarios: list[str] = []

    def __call__(self, call: FakeCall) -> dict:
        refs = referenced_ids(call)
        batch = []
        for _ in range(3):
            text = f"{self.prefix} #{next(self._n)}: 고객 데이터를 정리해 보고하는 업무 상황"
            self.scenarios.append(text)
            batch.append(candidate(text, refs=refs))
        return {"candidates": batch}


def scripted_batch(*scenarios: str) -> Callable[[FakeCall], dict]:
    """A one-shot responder returning exactly these scenarios (referencing shown tasks)."""
    return lambda call: {"candidates": [candidate(s, refs=referenced_ids(call)) for s in scenarios]}


def by_schema(responders: Mapping[type[BaseModel], Callable[[FakeCall], Any]]):
    """Dispatch a role's calls to a responder chosen by the call's ``schema``."""

    def respond(call: FakeCall) -> Any:
        if call.schema not in responders:
            raise AssertionError(f"no responder for schema {call.schema}")
        return responders[call.schema](call)

    return respond


def start_awaiting_selection(
    service: Any,
    fake: FakeGateway,
    db: Database,
    settings: Settings,
    *,
    author_id: int = 1,
    input: DraftInput | None = None,
    writer: ScenarioWriter | None = None,
) -> Draft:
    """Arrange a draft in 상황 선택 대기 (seeds 유사 과제, scripts candidate generation).

    Installs ``writer`` (default a fresh ``ScenarioWriter``) as the GENERATION
    fallback only for ``CandidateBatch`` calls if no GENERATION fallback exists yet.
    """
    seed_similar_tasks(db, fake, settings)
    if not fake.has_fallback(Role.GENERATION):
        fake.respond_with(Role.GENERATION, by_schema({CandidateBatch: writer or ScenarioWriter()}))
    return service.start(input or make_input(), author_id=author_id)
