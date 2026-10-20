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
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
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
from task_design.workflow import prompts
from task_design.workflow.schemas import (
    AttachmentBatch,
    CandidateBatch,
    InstructionDraft,
    RubricDraft,
)

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


# --- 생성 · 모의 응시 · 채점 (ticket 06) ------------------------------------------

Scores = Mapping[Level, float | Sequence[float]]

# 상 85 / 중 60 / 하 30, each graded identically 3 times: passes every check.
PASSING_SCORES: dict[Level, float] = {Level.HIGH: 85, Level.MEDIUM: 60, Level.LOW: 30}

# Points of the rubric items TaskWriter writes (과정 30, 산출물 40, 산출물 30).
RUBRIC_POINTS = (30, 40, 30)


def output_tag(level: Level) -> str:
    """Marker the ``Personas`` responder puts in each persona's final output."""
    return f"⟦persona:{level.value}⟧"


def persona_level(call: FakeCall) -> Level:
    """The level of the persona making an EXAMINEE call."""
    for level in Level:
        if prompts.persona_tag(level) in call.prompt:
            return level
    raise AssertionError("not a persona prompt")


def submission_level(call: FakeCall) -> Level:
    """The level of the persona whose submission a GRADING call grades (needs ``Personas``)."""
    for level in Level:
        if output_tag(level) in call.prompt:
            return level
    raise AssertionError("grading prompt has no Personas output tag")


class TaskWriter:
    """GENERATION responder for 지시문 / 첨부 자료 / 채점 기준 calls.

    Each call returns a new numbered version, so a regenerated part is
    distinguishable (``instructions`` / ``rubrics`` list everything written).
    Rubric items use ``RUBRIC_POINTS`` and the given competency ids.
    """

    def __init__(self, competency_ids: Sequence[str] = ("C1",)) -> None:
        self.competency_ids = list(competency_ids)
        self.instructions: list[str] = []
        self.rubrics: list[dict] = []
        self.attachments: list[dict] = []
        self._lock = threading.Lock()

    def responders(self) -> dict[type[BaseModel], Callable[[FakeCall], Any]]:
        return {InstructionDraft: self.instruction, AttachmentBatch: self.attachment_batch,
                RubricDraft: self.rubric}

    def instruction(self, call: FakeCall) -> dict:
        with self._lock:
            text = f"지시문 v{len(self.instructions) + 1}: 첨부한 매출 CSV를 검증해 요약 보고서를 제출하라."
            self.instructions.append(text)
        return {"instruction": text}

    def attachment_batch(self, call: FakeCall) -> dict:
        with self._lock:
            n = len(self.attachments) + 1
            batch = {"attachments": [{
                "filename": f"sales_v{n}.csv",
                "format": "csv",
                "content": "월,매출\n1월,100\n2월,120\n2월,120\n합계,400",
                "traps": [{"location": "4행", "description": "2월 행 중복"},
                          {"location": "5행 합계", "description": "합계가 실제(340)와 다름"}],
            }]}
            self.attachments.append(batch)
        return batch

    def rubric(self, call: FakeCall) -> dict:
        with self._lock:
            n = len(self.rubrics) + 1
            types = ["process", "output", "output"]
            items = [{
                "title": f"항목 {i + 1} (v{n})",
                "competency_id": self.competency_ids[i % len(self.competency_ids)],
                "type": types[i],
                "points": points,
                "criteria_zero": f"v{n} 0점 기준",
                "criteria_partial": f"v{n} 부분 기준",
                "criteria_full": f"v{n} 만점 기준",
            } for i, points in enumerate(RUBRIC_POINTS)]
            rubric = {"items": items}
            self.rubrics.append(rubric)
        return rubric


CONSISTENT_REVIEW = {"consistent": True, "target": None, "issues": []}


class Personas:
    """EXAMINEE responder: each level asks the AI 도구 ``asks[level]`` times, then submits.

    Defaults follow the spec's behaviours: 하 1 (paste and submit), 중 2, 상 4.
    The final output carries ``output_tag(level)`` so ``Grader`` can tell
    submissions apart. ``before_call(level, call)``, if given, runs first on
    every call (e.g. to observe ``get_draft`` mid-run or to wait on a barrier).
    """

    DEFAULT_ASKS = {Level.LOW: 1, Level.MEDIUM: 2, Level.HIGH: 4}

    def __init__(
        self,
        asks: Mapping[Level, int] | None = None,
        before_call: Callable[[Level, FakeCall], None] | None = None,
    ) -> None:
        self.asks = {**self.DEFAULT_ASKS, **(asks or {})}
        self.before_call = before_call

    def __call__(self, call: FakeCall) -> dict:
        level = persona_level(call)
        if self.before_call is not None:
            self.before_call(level, call)
        asked = sum(m.role == "assistant" for m in call.messages)
        if asked < self.asks[level]:
            # Tagged too: a persona out of turns has its last ask taken as the output.
            return {"action": "ask", "content": f"{level.ko} 수준 요청 {asked + 1} {output_tag(level)}"}
        return {"action": "submit", "content": f"최종 산출물 {output_tag(level)}"}


def ai_tool_reply(call: FakeCall) -> str:
    return f"AI 도구 응답 {len(call.messages)}"


class Grader:
    """GRADING responder with scripted totals per persona level.

    ``scores[level]`` is a total (every grading gets it) or a sequence of totals
    consumed in call order for that level, 3 per round (gradings of one round
    run in parallel, so only the multiset per round matters). Once a sequence
    is exhausted the last ``gradings_per_submission`` values repeat, so
    ``[r1, r1, r1, r2, r2, r2]`` scripts two rounds and every later round
    reuses round 2. Each total is split over the items in proportion to
    ``RUBRIC_POINTS``.
    """

    def __init__(self, scores: Scores = PASSING_SCORES, *, per_round: int = 3) -> None:
        self._scores = {
            lv: [float(v)] * per_round if isinstance(v, int | float) else [float(x) for x in v]
            for lv, v in scores.items()
        }
        self._per_round = per_round
        self._used: dict[Level, int] = dict.fromkeys(Level, 0)
        self._lock = threading.Lock()

    def total_for(self, level: Level) -> float:
        with self._lock:
            values, n = self._scores[level], self._used[level]
            self._used[level] += 1
        if n < len(values):
            return values[n]
        tail = values[-self._per_round:]
        return tail[(n - len(values)) % len(tail)]

    def __call__(self, call: FakeCall) -> dict:
        total = self.total_for(submission_level(call))
        whole = sum(RUBRIC_POINTS)
        return {"item_scores": [
            {"item_index": i, "score": total * points / whole, "rationale": f"항목 {i} 근거"}
            for i, points in enumerate(RUBRIC_POINTS)
        ]}


@dataclass
class VerificationScript:
    """What ``install_verification`` installed (for inspection in tests)."""

    task_writer: TaskWriter
    scenario_writer: ScenarioWriter
    personas: Personas
    grader: Grader


def install_verification(
    fake: FakeGateway,
    *,
    scores: Scores = PASSING_SCORES,
    competency_ids: Sequence[str] = ("C1",),
    personas: Personas | None = None,
    task_writer: TaskWriter | None = None,
) -> VerificationScript:
    """Install fallbacks for every role the select_scenario run uses.

    Only roles without a fallback get one, so install your own
    ``fake.respond_with`` first to override a role (and ``fake.script`` to
    queue one-off answers ahead of the fallback, e.g. a REVIEW finding).
    GENERATION gets ``by_schema`` over ``ScenarioWriter`` + ``TaskWriter``,
    REVIEW answers consistent, EXAMINEE ``Personas``, AI_TOOL a canned reply,
    GRADING ``Grader(scores)``.
    """
    script = VerificationScript(
        task_writer=task_writer or TaskWriter(competency_ids),
        scenario_writer=ScenarioWriter(),
        personas=personas or Personas(),
        grader=Grader(scores),
    )
    defaults: dict[Role, Callable[[FakeCall], Any]] = {
        Role.GENERATION: by_schema({CandidateBatch: script.scenario_writer,
                                    **script.task_writer.responders()}),
        Role.REVIEW: lambda call: CONSISTENT_REVIEW,
        Role.EXAMINEE: script.personas,
        Role.AI_TOOL: ai_tool_reply,
        Role.GRADING: script.grader,
    }
    for role, responder in defaults.items():
        if not fake.has_fallback(role):
            fake.respond_with(role, responder)
    return script


def start_awaiting_review(
    service: Any,
    fake: FakeGateway,
    db: Database,
    settings: Settings,
    *,
    scores: Scores = PASSING_SCORES,
    author_id: int = 1,
    input: DraftInput | None = None,
    candidate_index: int = 0,
    personas: Personas | None = None,
) -> Draft:
    """Arrange a draft in 검토 대기: start → select candidate → generation → 모의 응시 → 판정.

    ``scores`` scripts the graders (see ``Grader``); the default passes round 1.
    Other roles are scripted by ``install_verification`` unless the test
    installed its own fallbacks first. Returns the draft as ``select_scenario``
    left it (with ``service.run_graph`` inline, that is 검토 대기).
    """
    input = input or make_input()
    install_verification(fake, scores=scores, competency_ids=input.competency_ids,
                         personas=personas)
    draft = start_awaiting_selection(service, fake, db, settings, author_id=author_id, input=input)
    return service.select_scenario(draft.id, draft.candidates[candidate_index].id)
