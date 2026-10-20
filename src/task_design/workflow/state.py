"""Graph state and shared node dependencies.

State values are plain JSON (``model_dump(mode="json")`` dicts, str uuids) so
the Postgres checkpointer serialises them without pickling domain classes.
Use the helpers below to get domain objects back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import UUID

from task_design.db import Database
from task_design.domain import DraftInput, ScenarioCandidate, Task
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


@dataclass(frozen=True)
class Deps:
    db: Database
    gateway: ModelGateway
    settings: Settings
    drafts: DraftStore
    tasks: TaskRepository
    competencies: CompetencyRepository


def draft_id(state: WorkflowState) -> UUID:
    return UUID(state["draft_id"])


def draft_input(state: WorkflowState) -> DraftInput:
    return DraftInput.model_validate(state["input"])


def similar_tasks(state: WorkflowState) -> list[Task]:
    return [Task.model_validate(t) for t in state.get("similar_tasks", [])]


def candidates(state: WorkflowState) -> list[ScenarioCandidate]:
    return [ScenarioCandidate.model_validate(c) for c in state.get("candidates", [])]


def dump(model: Any) -> dict[str, Any]:
    return model.model_dump(mode="json")
