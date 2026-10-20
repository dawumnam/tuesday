"""모의 응시 → 채점 → 판정 (ADR 0001).

Parallelism is LangGraph fan-out: ``fan_out_personas`` sends one ``run_persona``
task per level and ``fan_out_gradings`` one ``grade_submission`` task per
(level, grading index). LangGraph runs the tasks of a superstep concurrently
and checkpoints each finished task's writes, so a crash mid-fan-out only reruns
the unfinished ones.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Send

from task_design.domain import (
    ChatTurn,
    Component,
    DraftRound,
    Grading,
    Level,
    MockExamResult,
    PersonaStatus,
    Task,
    Verdict,
)
from task_design.gateway import Role
from task_design.settings import Settings
from task_design.workflow import prompts
from task_design.workflow.nodes.generation import complete_checked
from task_design.workflow.schemas import ExamineeAction, GradingDraft
from task_design.workflow.state import (
    Deps,
    GradingJob,
    PersonaJob,
    WorkflowState,
    draft_id,
    draft_input,
    dump,
    round_entries,
    task,
)

# Node names the fan-out edges target (graph.N re-exports them).
RUN_PERSONA = "run_persona"
GRADE_SUBMISSION = "grade_submission"


def start_mock_exam(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """Entry of a round's 모의 응시 (round 1, and every fix round in ticket 07)."""
    deps.drafts.reset_persona_status(draft_id(state))
    return {}


def fan_out_personas(state: WorkflowState) -> list[Send]:
    return [
        Send(RUN_PERSONA, PersonaJob(draft_id=state["draft_id"], input=state["input"],
                                     task=state["task"], round=state["round"], level=level.value))
        for level in Level
    ]


def run_persona(deps: Deps, job: PersonaJob) -> dict[str, Any]:
    """One 응시자 페르소나 solves the task with the AI 도구 LLM (≤ max_examinee_turns messages)."""
    did, level, the_task = draft_id(job), Level(job["level"]), task(job)
    input = draft_input(job)
    max_turns = deps.settings.max_examinee_turns
    indicators = [i for cid in input.competency_ids
                  for i in deps.competencies.level_indicators(cid, level)]
    deps.drafts.set_persona_status(did, level, PersonaStatus.RUNNING)

    transcript: list[ChatTurn] = []
    output: str | None = None
    asked = 0
    while output is None:
        move = deps.gateway.complete(
            Role.EXAMINEE,
            prompts.persona_turn(level, input, indicators, the_task, transcript,
                                 max_turns=max_turns),
            schema=ExamineeAction,
            draft_id=did,
        )
        if move.action == "submit" or asked >= max_turns:
            output = move.content  # out of turns: whatever it says is the submission
            break
        transcript.append(ChatTurn(speaker="examinee", content=move.content))
        asked += 1
        reply = deps.gateway.complete(Role.AI_TOOL, prompts.ai_tool_turn(transcript), draft_id=did)
        transcript.append(ChatTurn(speaker="ai_tool", content=reply))

    deps.drafts.set_persona_status(did, level, PersonaStatus.DONE)
    return {"submissions": [{"round": job["round"], "level": level.value,
                             "transcript": [dump(t) for t in transcript], "output": output}]}


def start_grading(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """Join point after the three personas; fans out the gradings."""
    return {}


def fan_out_gradings(state: WorkflowState, *, gradings_per_submission: int) -> list[Send]:
    subs = round_entries(state.get("submissions", []), state["round"], "level")
    return [
        Send(GRADE_SUBMISSION, GradingJob(
            draft_id=state["draft_id"], task=state["task"], round=state["round"],
            level=s["level"], index=i, transcript=s["transcript"], output=s["output"]))
        for s in subs
        for i in range(gradings_per_submission)
    ]


def grading_problems(grading: GradingDraft, the_task: Task) -> list[str]:
    items = the_task.rubric.items
    problems = []
    indices = [s.item_index for s in grading.item_scores]
    if sorted(indices) != list(range(len(items))):
        problems.append(f"item_index는 0~{len(items) - 1}를 한 번씩 모두 채점해야 합니다 "
                        f"(받은 값: {indices})")
    for s in grading.item_scores:
        if 0 <= s.item_index < len(items) and s.score > items[s.item_index].points:
            problems.append(f"{s.item_index}번 항목 점수 {s.score}가 배점 "
                            f"{items[s.item_index].points}점을 넘습니다")
    return problems


def grade_submission(deps: Deps, job: GradingJob) -> dict[str, Any]:
    """One independent grading of one submission, per-item score + rationale."""
    the_task = task(job)
    transcript = [ChatTurn.model_validate(t) for t in job["transcript"]]
    result = complete_checked(
        deps, Role.GRADING, prompts.grading(the_task, transcript, job["output"]),
        GradingDraft, lambda g: grading_problems(g, the_task), draft_id=draft_id(job),
    )
    grading = Grading(item_scores=sorted(result.item_scores, key=lambda s: s.item_index))
    return {"gradings": [{"round": job["round"], "level": job["level"], "index": job["index"],
                          "grading": dump(grading)}]}


def judge_results(results: list[MockExamResult], settings: Settings) -> Verdict:
    """판정: 풀이 가능성, 변별력, 채점 일관성 against the settings thresholds."""
    score = {r.persona_level: r.mean_score for r in results}
    high, medium, low = score[Level.HIGH], score[Level.MEDIUM], score[Level.LOW]
    return Verdict(
        solvable=high >= settings.solvable_min_score,
        discriminating=high > medium > low and high - low >= settings.discrimination_min_gap,
        consistent=all(r.score_stdev <= settings.grading_max_stdev for r in results),
    )


def round_results(state: WorkflowState) -> list[MockExamResult]:
    """The current round's 모의 응시 결과, ordered 상·중·하."""
    rnd = state["round"]
    subs = {s["level"]: s for s in round_entries(state.get("submissions", []), rnd, "level")}
    gradings = round_entries(state.get("gradings", []), rnd, "level", "index")
    results = []
    for level in Level:
        sub = subs[level.value]
        mine = sorted((g for g in gradings if g["level"] == level.value), key=lambda g: g["index"])
        results.append(MockExamResult(
            persona_level=level,
            transcript=[ChatTurn.model_validate(t) for t in sub["transcript"]],
            output=sub["output"],
            gradings=[Grading.model_validate(g["grading"]) for g in mine],
        ))
    return results


def judge(deps: Deps, state: WorkflowState) -> dict[str, Any]:
    """판정, and persist the round (number, changes, 모의 응시 결과, verdict)."""
    results = round_results(state)
    verdict = judge_results(results, deps.settings)
    changes = state["round_changes"]
    deps.drafts.save_round(draft_id(state), DraftRound(
        number=state["round"],
        changed_components=[Component(c) for c in changes["components"]],
        change_reason=changes["reason"],
        mock_exam_results=results,
        verdict=verdict,
    ))
    return {"verdict": dump(verdict), "verified_round": state["round"]}


