"""생성 nodes: 지시문 작성 → 첨부 자료 작성 → 채점 기준 작성 → 검토 에이전트."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from task_design.domain import (
    Attachment,
    Competency,
    Component,
    DraftInput,
    Rubric,
    Task,
)
from task_design.gateway import Message, Role
from task_design.workflow import prompts
from task_design.workflow.errors import InvalidModelContent
from task_design.workflow.schemas import (
    AttachmentBatch,
    InstructionDraft,
    ReviewFindings,
    RubricDraft,
)
from task_design.workflow.state import Deps, WorkflowState, candidates, draft_id, draft_input, dump

M = TypeVar("M", bound=BaseModel)


def complete_checked(
    deps: Deps,
    role: Role,
    messages: Sequence[Message],
    schema: type[M],
    check: Callable[[M], list[str]],
    *,
    draft_id: Any,
) -> M:
    """``gateway.complete`` plus domain checks the JSON schema can't express.

    When ``check`` returns problems, they are fed back to the model and the call
    is retried, up to ``settings.schema_retries`` extra attempts.
    """
    msgs = list(messages)
    problems: list[str] = []
    for _ in range(deps.settings.schema_retries + 1):
        result = deps.gateway.complete(role, msgs, schema=schema, draft_id=draft_id)
        problems = check(result)
        if not problems:
            return result
        msgs += [
            Message(role="assistant", content=result.model_dump_json()),
            Message(role="user", content="다음 문제를 고쳐 다시 작성하세요:\n"
                    + "\n".join(f"- {p}" for p in problems)),
        ]
    raise InvalidModelContent(
        f"모델 응답이 규칙을 계속 어겼습니다 ({schema.__name__}): {'; '.join(problems)}")


def _competencies(deps: Deps, input: DraftInput) -> list[Competency]:
    return [c for cid in input.competency_ids if (c := deps.competencies.get(cid))]


def rubric_problems(rubric: Rubric, input: DraftInput) -> list[str]:
    """Domain rules beyond ``Rubric``'s own shape check."""
    problems = []
    unknown = {i.competency_id for i in rubric.items} - set(input.competency_ids)
    if unknown:
        problems.append(f"competency_id는 {', '.join(input.competency_ids)} 중 하나여야 합니다 "
                        f"(잘못된 값: {', '.join(sorted(unknown))})")
    if rubric.total_points != 100:
        problems.append(f"배점 합계가 100점이어야 합니다 (현재 {rubric.total_points}점)")
    return problems


def write_instruction_text(
    deps: Deps, state: WorkflowState, *, previous: str | None = None, issues: Sequence[str] = ()
) -> str:
    input = draft_input(state)
    result = deps.gateway.complete(
        Role.GENERATION,
        prompts.instruction(input, _competencies(deps, input), state["scenario"],
                            previous=previous, issues=issues),
        schema=InstructionDraft,
        draft_id=draft_id(state),
    )
    return result.instruction


def write_rubric_model(
    deps: Deps,
    state: WorkflowState,
    *,
    instruction: str,
    attachments: Sequence[Attachment],
    previous: Rubric | None = None,
    issues: Sequence[str] = (),
) -> Rubric:
    input = draft_input(state)
    result = complete_checked(
        deps,
        Role.GENERATION,
        prompts.rubric(input, _competencies(deps, input), state["scenario"], instruction,
                       attachments, previous=previous, issues=issues),
        RubricDraft,
        lambda r: rubric_problems(r.to_rubric(), input),
        draft_id=draft_id(state),
    )
    return result.to_rubric()


def write_attachment_models(
    deps: Deps,
    state: WorkflowState,
    *,
    instruction: str,
    previous: Sequence[Attachment] = (),
    issues: Sequence[str] = (),
) -> list[Attachment]:
    """첨부 자료 with marked 함정; ``previous`` + ``issues`` rewrites existing ones."""
    input = draft_input(state)
    batch = deps.gateway.complete(
        Role.GENERATION,
        prompts.attachments(input, _competencies(deps, input), state["scenario"], instruction,
                            previous=previous, issues=issues),
        schema=AttachmentBatch,
        draft_id=draft_id(state),
    )
    return list(batch.attachments)


# --- nodes -------------------------------------------------------------------


def write_instruction(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """First node after selection: fixes the chosen 상황 and writes the 지시문."""
    chosen = next(c for c in candidates(state) if str(c.id) == state["selected_candidate_id"])
    state = {**state, "scenario": chosen.scenario}
    return {"scenario": chosen.scenario, "instruction": write_instruction_text(deps, state)}


def write_attachments(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    attachments = write_attachment_models(deps, state, instruction=state["instruction"])
    return {"attachments": [dump(a) for a in attachments]}


def write_rubric(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    attachments = [Attachment.model_validate(a) for a in state["attachments"]]
    rubric = write_rubric_model(deps, state, instruction=state["instruction"],
                                attachments=attachments)
    return {"rubric": dump(rubric)}


def _assemble(state: WorkflowState, instruction: str, rubric: Rubric) -> Task:
    input = draft_input(state)
    return Task(
        scenario=state["scenario"],
        instruction=instruction,
        attachments=[Attachment.model_validate(a) for a in state["attachments"]],
        rubric=rubric,
        competency_ids=input.competency_ids,
        audience=input.audience,
        difficulty=input.difficulty,
    )


def review_task(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """검토 에이전트: check 지시문 ↔ 채점 기준; on a finding, regenerate that part once.

    Ends generation: stores the assembled task on the draft and opens round 1.
    """
    did = draft_id(state)
    instruction = state["instruction"]
    rubric = Rubric.model_validate(state["rubric"])
    findings = deps.gateway.complete(
        Role.REVIEW, prompts.review(_assemble(state, instruction, rubric)),
        schema=ReviewFindings, draft_id=did,
    )
    reason = "초기 생성: 고른 상황으로 지시문, 첨부 자료, 채점 기준을 작성"
    if not findings.consistent and findings.target == "instruction":
        instruction = write_instruction_text(deps, state, previous=instruction,
                                             issues=findings.issues)
        reason += f". 검토 에이전트 지적으로 지시문 재작성: {'; '.join(findings.issues)}"
    elif not findings.consistent and findings.target == "rubric":
        rubric = write_rubric_model(
            deps, state, instruction=instruction,
            attachments=[Attachment.model_validate(a) for a in state["attachments"]],
            previous=rubric, issues=findings.issues,
        )
        reason += f". 검토 에이전트 지적으로 채점 기준 재작성: {'; '.join(findings.issues)}"

    task = _assemble(state, instruction, rubric)
    deps.drafts.set_task(did, task)
    return {
        "instruction": instruction,
        "rubric": dump(rubric),
        "review_findings": dump(findings),
        "task": dump(task),
        "round": 1,
        "round_changes": {
            "components": [Component.INSTRUCTION.value, Component.ATTACHMENTS.value,
                           Component.RUBRIC.value],
            "reason": reason,
        },
    }
