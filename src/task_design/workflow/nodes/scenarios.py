"""상황 설계 nodes: 유사 과제 검색 → 상황 후보 생성과 중복 검사 → 중간 사람 확인."""

from __future__ import annotations

import math
from typing import Any
from uuid import UUID

from langgraph.types import interrupt

from task_design.domain import DraftState, ScenarioCandidate
from task_design.gateway import Role, Vector
from task_design.workflow import prompts
from task_design.workflow.schemas import CandidateBatch, CandidateProposal
from task_design.workflow.state import (
    Deps,
    WorkflowState,
    candidates,
    draft_id,
    draft_input,
    dump,
    similar_tasks,
)

CANDIDATE_COUNT = 3


def _cosine(a: Vector, b: Vector) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def search_similar_tasks(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    did, input = draft_id(state), draft_input(state)
    comps = [c for cid in input.competency_ids if (c := deps.competencies.get(cid))]
    found = deps.tasks.hybrid_search(
        prompts.similar_task_query(input, comps), k=deps.settings.similar_tasks_k, draft_id=did
    )
    deps.drafts.set_similar_tasks(did, found)
    return {"similar_tasks": [dump(t) for t in found]}


def generate_candidates(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """Generate 3 mutually distinct 상황 후보, none a duplicate of a task in the DB.

    A proposal whose scenario embedding is within ``duplicate_threshold`` of an
    existing task or of an already-accepted candidate is dropped and replaced by
    another generation call, up to ``candidate_generation_attempts`` calls. If
    the budget runs out, the slots are filled with the least bad rejects.
    """
    did, input, similar = draft_id(state), draft_input(state), similar_tasks(state)
    comps = [c for cid in input.competency_ids if (c := deps.competencies.get(cid))]
    similar_ids = {str(t.id) for t in similar}
    threshold = deps.settings.duplicate_threshold
    shown = list(state.get("shown_scenarios", []))

    accepted: list[tuple[CandidateProposal, Vector]] = []
    rejected: list[tuple[float, CandidateProposal]] = []
    for _ in range(max(1, deps.settings.candidate_generation_attempts)):
        needed = CANDIDATE_COUNT - len(accepted)
        avoid = shown + [p.scenario for p, _ in accepted] + [p.scenario for _, p in rejected]
        batch = deps.gateway.complete(
            Role.GENERATION,
            prompts.scenario_candidates(input, comps, similar, count=needed, avoid=avoid),
            schema=CandidateBatch,
            draft_id=did,
        )
        proposals = batch.candidates[:needed]
        if not proposals:
            continue
        vectors = deps.gateway.embed([p.scenario for p in proposals], draft_id=did)
        for proposal, vector in zip(proposals, vectors, strict=True):
            db_dups = deps.tasks.find_duplicates_of_vector(vector, threshold)
            peer = max((_cosine(vector, v) for _, v in accepted), default=0.0)
            worst = max([peer] + [d.similarity for d in db_dups])
            if worst >= threshold or proposal.scenario in shown:
                rejected.append((worst, proposal))
            else:
                accepted.append((proposal, vector))
        if len(accepted) == CANDIDATE_COUNT:
            break

    chosen = [p for p, _ in accepted]
    chosen += [p for _, p in sorted(rejected, key=lambda r: r[0])][: CANDIDATE_COUNT - len(chosen)]
    result = [
        ScenarioCandidate(
            title=p.title,
            scenario=p.scenario,
            referenced_task_ids=[UUID(r) for r in dict.fromkeys(p.referenced_task_ids)
                                 if r in similar_ids],
        )
        for p in chosen
    ]
    deps.drafts.set_candidates(did, result)
    return {"candidates": [dump(c) for c in result]}


def await_selection(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """중간 사람 확인. Resume with ``{"action": "regenerate"}`` or
    ``{"action": "select", "candidate_id": "<uuid>"}``.

    The interrupt payload's ``state`` is what the runner records once the run
    has paused (and the checkpoint is saved).
    """
    decision = interrupt({"state": DraftState.AWAITING_SCENARIO_SELECTION.value})
    if decision["action"] == "regenerate":
        offered = [c.scenario for c in candidates(state)]
        return {
            "selection": "regenerate",
            "shown_scenarios": [*state.get("shown_scenarios", []), *offered],
        }
    return {"selection": "select", "selected_candidate_id": str(decision["candidate_id"])}


def route_after_selection(state: WorkflowState) -> str:
    return state["selection"]
