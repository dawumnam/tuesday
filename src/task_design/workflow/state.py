"""Graph state and shared node dependencies.

State values are plain JSON (``model_dump(mode="json")`` dicts, str uuids) so
the Postgres checkpointer serialises them without pickling domain classes.
Use the helpers below to get domain objects back.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID

from task_design.db import Database
from task_design.domain import DraftInput, ScenarioCandidate, Task, Verdict
from task_design.gateway import ModelGateway
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.settings import Settings
from task_design.workflow.drafts import DraftStore


class WorkflowState(TypedDict, total=False):
    draft_id: str
    input: dict[str, Any]  # DraftInput
    similar_tasks: list[dict[str, Any]]  # list[Task] with ids
    candidates: list[dict[str, Any]]  # list[ScenarioCandidate], the batch on offer
    shown_scenarios: list[str]  # every scenario offered so far (regeneration avoids them)
    selection: Literal["regenerate", "select"]  # the human's last decision
    selected_candidate_id: str

    # --- 생성 (ticket 06) ---
    scenario: str  # the chosen 상황 (never changed after selection)
    instruction: str
    attachments: list[dict[str, Any]]  # list[Attachment]
    rubric: dict[str, Any]  # Rubric
    review_findings: dict[str, Any]  # schemas.ReviewFindings
    task: dict[str, Any]  # Task: the current content the 모의 응시 runs on

    # --- 회차 / 모의 응시 / 판정 ---
    # The round being verified. Round 1 = initial generation. Ticket 07's fix
    # nodes update ``task``, bump ``round`` and set ``round_changes`` before
    # re-entering START_MOCK_EXAM.
    round: int
    round_changes: dict[str, Any]  # {"components": [Component values], "reason": str}
    # Fan-in from the parallel persona / grading nodes. Append-only across
    # rounds; every entry carries its "round", so read them with round_entries().
    submissions: Annotated[list[dict[str, Any]], operator.add]
    gradings: Annotated[list[dict[str, Any]], operator.add]
    verdict: dict[str, Any]  # Verdict of the latest verified round
    verified_round: int  # number of that round (≠ round: the current round is unverified)
    # --- 자기 수정 루프 (ticket 07) ---
    discrimination_fixes: int  # 변별력 부족 fixes so far (채점 기준 and 함정 alternate)


class PersonaJob(TypedDict):
    """Input of one parallel persona run (sent via ``Send``)."""

    draft_id: str
    input: dict[str, Any]
    task: dict[str, Any]
    round: int
    level: str  # Level value


class GradingJob(TypedDict):
    """Input of one independent grading (sent via ``Send``)."""

    draft_id: str
    task: dict[str, Any]
    round: int
    level: str  # Level value of the graded persona
    index: int  # 0..gradings_per_submission-1
    transcript: list[dict[str, Any]]  # list[ChatTurn]
    output: str


@dataclass(frozen=True)
class Deps:
    db: Database
    gateway: ModelGateway
    settings: Settings
    drafts: DraftStore
    tasks: TaskRepository
    competencies: CompetencyRepository


def draft_id(state: WorkflowState | PersonaJob | GradingJob) -> UUID:
    return UUID(state["draft_id"])


def draft_input(state: WorkflowState | PersonaJob) -> DraftInput:
    return DraftInput.model_validate(state["input"])


def similar_tasks(state: WorkflowState) -> list[Task]:
    return [Task.model_validate(t) for t in state.get("similar_tasks", [])]


def candidates(state: WorkflowState) -> list[ScenarioCandidate]:
    return [ScenarioCandidate.model_validate(c) for c in state.get("candidates", [])]


def task(state: WorkflowState | PersonaJob | GradingJob) -> Task:
    return Task.model_validate(state["task"])


def verdict(state: WorkflowState) -> Verdict | None:
    return Verdict.model_validate(state["verdict"]) if state.get("verdict") else None


def round_entries(entries: list[dict[str, Any]], round: int, *key: str) -> list[dict[str, Any]]:
    """Entries of ``round``, de-duplicated by the ``key`` fields (last write wins)."""
    out: dict[tuple, dict[str, Any]] = {}
    for e in entries:
        if e["round"] == round:
            out[tuple(e[k] for k in key)] = e
    return list(out.values())


def dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")
