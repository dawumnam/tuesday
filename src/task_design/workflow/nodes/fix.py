"""자기 수정 루프: after a failed 판정, regenerate one component and verify again.

``route_after_verdict`` decides (pass → finish; failure with fixes
(``settings.max_fix_rounds``) and token budget left → fix; otherwise finish with a
실패 리포트). ``route_to_mock_exam`` checks the token budget again before every
round's 모의 응시 (round 1 and after each fix): over budget, the draft goes to
review unverified, with a 실패 리포트. ``fix`` handles the first failing type in
order (풀이 불가 → 지시문; 변별력 부족 → 채점 기준, then 함정 with the 판정 서술
re-synced to them, in turn; 채점 불일치 → 채점 기준의 판정 서술 only), then the
graph goes back to the 모의 응시. The 상황 is never touched.
"""

from __future__ import annotations

import statistics
from typing import Any

from task_design.costs import CostStore
from task_design.domain import (
    Attachment,
    Component,
    DraftState,
    FailureReport,
    FailureType,
    Level,
    MockExamResult,
    Rubric,
    Task,
    Verdict,
)
from task_design.gateway import Role
from task_design.settings import Settings
from task_design.workflow import prompts
from task_design.workflow.nodes import generation
from task_design.workflow.nodes.mock_exam import round_results
from task_design.workflow.schemas import AttachmentBatch, RubricDraft
from task_design.workflow.state import (
    Deps,
    WorkflowState,
    draft_id,
    draft_input,
    dump,
    task,
)
from task_design.workflow.state import verdict as current_verdict

FIX = "fix"
FINISH = "finish"
MOCK_EXAM = "mock_exam"


def draft_tokens(deps: Deps, state: WorkflowState) -> int:
    """Tokens the draft has used so far, as recorded by the gateway's usage sink."""
    return sum(c.input_tokens + c.output_tokens for c in CostStore(deps.db).for_draft(draft_id(state)))


def over_budget(deps: Deps, state: WorkflowState) -> bool:
    return draft_tokens(deps, state) > deps.settings.draft_token_budget


def route_to_mock_exam(state: WorkflowState, *, deps: Deps) -> str:
    """Before each round's 모의 응시: "mock_exam" unless the token budget is used up."""
    return FINISH if over_budget(deps, state) else MOCK_EXAM


def route_after_verdict(state: WorkflowState, *, deps: Deps) -> str:
    """THE VERDICT EDGE: "fix" while failures remain and fixes and token budget are left."""
    verdict = current_verdict(state)
    if verdict is None or verdict.passed:
        return FINISH
    if _fixes_done(state) >= deps.settings.max_fix_rounds:
        return FINISH
    if over_budget(deps, state):
        return FINISH
    return FIX


def _fixes_done(state: WorkflowState) -> int:
    return state["round"] - 1


def failure_report(deps: Deps, state: WorkflowState) -> FailureReport | None:
    """실패 리포트 for a draft that stops while failing or unverified, else None.

    Unverified: the token budget ran out before the current round's 모의 응시 (its
    changes, if it is a fix round, count as an attempted fix).
    """
    verdict = current_verdict(state)
    unverified = state.get("verified_round") != state["round"]
    if not unverified and (verdict is None or verdict.passed):
        return None
    rounds = deps.drafts.get(draft_id(state)).rounds
    attempted = [rnd.change_reason for rnd in rounds if rnd.number > 1]
    if unverified and state["round"] > 1:
        attempted.append(state["round_changes"]["reason"])
    return FailureReport(
        failure_types=verdict.failures if verdict else [],
        scores_by_round=[
            {r.persona_level: r.mean_score for r in rnd.mock_exam_results} for rnd in rounds
        ],
        attempted_fixes=attempted,
        stopped_by_token_budget=unverified
        or _fixes_done(state) < deps.settings.max_fix_rounds,
    )


def finish(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """검토 대기: the draft goes to 최종 검토 with its current task, rounds and, if the
    loop stopped while still failing, a 실패 리포트."""
    did = draft_id(state)
    deps.drafts.set_task(did, task(state))
    deps.drafts.set_failure_report(did, failure_report(deps, state))
    deps.drafts.set_state(did, DraftState.AWAITING_REVIEW)
    return {}


# --- 실패 근거 (fed to the regeneration prompt) ------------------------------------


def _item_means(result: MockExamResult) -> list[float]:
    by_item: dict[int, list[float]] = {}
    for g in result.gradings:
        for s in g.item_scores:
            by_item.setdefault(s.item_index, []).append(s.score)
    return [statistics.fmean(by_item[i]) for i in sorted(by_item)]


def failure_evidence(
    failure: FailureType, results: list[MockExamResult], rubric: Rubric, settings: Settings
) -> list[str]:
    """Korean bullet points describing why the round failed ``failure``."""
    by_level = {r.persona_level: r for r in results}
    means = " / ".join(f"{lv.ko} {by_level[lv].mean_score:.1f}점" for lv in Level)
    lines = [f"모의 응시 평균 점수: {means}"]
    if failure is FailureType.UNSOLVABLE:
        high = by_level[Level.HIGH]
        lines.append(f"상 수준 페르소나도 {high.mean_score:.1f}점으로 풀이 가능 기준 "
                     f"{settings.solvable_min_score}점에 못 미칩니다. 지시문이 무엇을 하고 "
                     "무엇을 제출해야 하는지 분명하지 않을 수 있습니다.")
        for i, (item, mean) in enumerate(zip(rubric.items, _item_means(high), strict=False)):
            if mean < item.points:
                rationale = high.gradings[0].item_scores[i].rationale
                lines.append(f"상 수준 '{item.title}' {mean:.1f}/{item.points}점: {rationale}")
        lines.append(f"상 수준 제출물: {high.output[:500]}")
    elif failure is FailureType.LOW_DISCRIMINATION:
        lines.append(f"상 > 중 > 하이고 상과 하의 차이가 {settings.discrimination_min_gap}점 "
                     "이상이어야 합니다. 수준별 행동 차이가 점수에 드러나지 않습니다.")
        for i, item in enumerate(rubric.items):
            per_level = " / ".join(f"{lv.ko} {_item_means(by_level[lv])[i]:.1f}" for lv in Level)
            lines.append(f"'{item.title}' ({item.points}점) 수준별 평균: {per_level}")
    else:  # INCONSISTENT_GRADING
        lines.append(f"같은 제출물을 3번 채점한 표준편차가 {settings.grading_max_stdev}점 "
                     "이하여야 합니다. 판정 서술이 채점자마다 다르게 읽힙니다.")
        for r in results:
            if r.score_stdev > settings.grading_max_stdev:
                totals = ", ".join(f"{g.total:.1f}" for g in r.gradings)
                lines.append(f"{r.persona_level.ko} 수준 제출물 채점 결과 {totals} "
                             f"(표준편차 {r.score_stdev:.1f})")
                for i, item in enumerate(rubric.items):
                    scores = [g.item_scores[i] for g in r.gradings]
                    if len({s.score for s in scores}) > 1:
                        lines.append(f"  '{item.title}': " + "; ".join(
                            f"{s.score:.1f}점 ({s.rationale})" for s in scores))
    return lines


# --- fix node -------------------------------------------------------------------

_CRITERIA_ONLY = ("항목 수, 제목, 유형, 배점, 역량은 그대로 두고 각 항목의 판정 기준 "
                  "(criteria_zero, criteria_partial, criteria_full) 문구만 고치세요.")


def _rewrite_criteria(deps: Deps, state: WorkflowState, the_task: Task, issues: list[str]) -> Rubric:
    """채점 불일치: rewrite only the 0점·부분·만점 판정 서술; items, types, points stay."""
    previous = the_task.rubric
    input = draft_input(state)
    issues = [*issues, _CRITERIA_ONLY]

    def problems(draft: RubricDraft) -> list[str]:
        out = generation.rubric_problems(draft.to_rubric(), input)
        if len(draft.items) != len(previous.items):
            out.append(f"항목 수는 {len(previous.items)}개 그대로여야 합니다")
        return out

    result = generation.complete_checked(
        deps, Role.GENERATION,
        prompts.rubric(input, generation._competencies(deps, input), the_task.scenario,
                       the_task.instruction, the_task.attachments,
                       previous=previous, issues=issues),
        RubricDraft, problems, draft_id=draft_id(state),
    )
    return Rubric(items=[
        old.model_copy(update={"criteria_zero": new.criteria_zero,
                               "criteria_partial": new.criteria_partial,
                               "criteria_full": new.criteria_full})
        for old, new in zip(previous.items, result.items, strict=True)
    ])


_TRAPS_ONLY = ("수준별 검증 행동의 차이가 드러나도록 함정만 다시 설계하세요. 첨부 자료의 수, 순서, "
               "파일 이름, 형식은 그대로 두고, 내용은 새 함정을 심는 데 필요한 부분만 고치세요.")
_RESYNC_CRITERIA = "첨부 자료의 함정이 바뀌었습니다. 판정 기준이 새 함정을 가리키도록 고치세요."


def _rewrite_traps(
    deps: Deps, state: WorkflowState, the_task: Task, issues: list[str]
) -> list[Attachment]:
    """변별력 부족 (함정 turn): new 함정 in the same files; files, order and formats stay."""
    previous = the_task.attachments

    def problems(batch: AttachmentBatch) -> list[str]:
        out = []
        if len(batch.attachments) != len(previous):
            out.append(f"첨부 자료는 {len(previous)}개 그대로여야 합니다")
        if not all(a.traps for a in batch.attachments):
            out.append("첨부 자료마다 함정을 1개 이상 표시해야 합니다")
        return out

    batch = generation.complete_checked(
        deps, Role.GENERATION,
        prompts.attachments(draft_input(state), generation._competencies(deps, draft_input(state)),
                            the_task.scenario, the_task.instruction, previous=previous,
                            issues=[*issues, _TRAPS_ONLY]),
        AttachmentBatch, problems, draft_id=draft_id(state),
    )
    return [new.model_copy(update={"filename": old.filename, "format": old.format})
            for old, new in zip(previous, batch.attachments, strict=True)]


def fix(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """Regenerate the component for the first failing type, then open the next round."""
    verdict: Verdict = current_verdict(state)
    failure = verdict.failures[0]
    the_task = task(state)
    issues = failure_evidence(failure, round_results(state), the_task.rubric, deps.settings)
    update: dict[str, Any] = {}

    if failure is FailureType.UNSOLVABLE:
        components, what = [Component.INSTRUCTION], "지시문 재작성"
        instruction = generation.write_instruction_text(
            deps, state, previous=the_task.instruction, issues=issues)
        the_task = the_task.model_copy(update={"instruction": instruction})
        update["instruction"] = instruction
    elif failure is FailureType.LOW_DISCRIMINATION:
        # 채점 기준 and 함정 take turns, starting with the 채점 기준.
        n = state.get("discrimination_fixes", 0)
        update["discrimination_fixes"] = n + 1
        if n % 2 == 0:
            components, what = [Component.RUBRIC], "채점 기준 재작성"
            rubric = generation.write_rubric_model(
                deps, state, instruction=the_task.instruction, attachments=the_task.attachments,
                previous=the_task.rubric, issues=issues)
            the_task = the_task.model_copy(update={"rubric": rubric})
            update["rubric"] = dump(rubric)
        else:
            # Only the 함정 change; then the criteria wording is re-synced to them.
            components = [Component.ATTACHMENTS, Component.RUBRIC]
            what = "첨부 자료의 함정 재작성과 채점 기준 판정 서술 재조정"
            attachments = _rewrite_traps(deps, state, the_task, issues)
            the_task = the_task.model_copy(update={"attachments": attachments})
            rubric = _rewrite_criteria(deps, state, the_task, [_RESYNC_CRITERIA])
            the_task = the_task.model_copy(update={"rubric": rubric})
            update["attachments"] = [dump(a) for a in attachments]
            update["rubric"] = dump(rubric)
    else:
        components, what = [Component.RUBRIC], "채점 기준의 판정 서술 재작성"
        rubric = _rewrite_criteria(deps, state, the_task, issues)
        the_task = the_task.model_copy(update={"rubric": rubric})
        update["rubric"] = dump(rubric)

    deps.drafts.set_task(draft_id(state), the_task)
    reason = f"{state['round']}회차 {failure.ko}: {what}. 근거: {' / '.join(issues[:2])}"
    return {
        **update,
        "task": dump(the_task),
        "round": state["round"] + 1,
        "round_changes": {"components": [c.value for c in components], "reason": reason},
    }

