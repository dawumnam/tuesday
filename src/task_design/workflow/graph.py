"""The LangGraph state graph and its runner.

Graph::

    START → search_similar_tasks → generate_candidates → await_selection ─┬─ regenerate → generate_candidates
                                                                          └─ select → write_instruction
    write_instruction → write_attachments → write_rubric → review_task
      → BUDGET CHECK ─┬─ over → finish
                      └─ start_mock_exam ══Send×3══▶ run_persona (상·중·하 in parallel)
      → start_grading   ══Send×9══▶ grade_submission (3 per submission, in parallel)
      → judge ── VERDICT EDGE (fix.route_after_verdict / VERDICT_ROUTES) ─┬─ finish → END
                                                                          └─ fix → BUDGET CHECK

자기 수정 루프: while the verdict fails and fixes (``settings.max_fix_rounds``) and
the draft's token budget remain, ``fix`` regenerates one component, bumps
``round`` and sets ``round_changes``; ``judge`` records the new round. The token
budget is also checked before every round's 모의 응시 (BUDGET CHECK). ``finish``
attaches the 실패 리포트 when the loop stopped while still failing or unverified.

Each node is ``fn(deps, state) -> partial state``; when the spec has a
``stage``, the draft's ``stage`` column is set before the node runs. With a run
observer installed (데모 모드), ``observer.before_step`` is called first at every
stage boundary (``NodeSpec.step``); it may block to pause the run there.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from task_design import tracing
from task_design.domain import DraftInput, DraftState, Stage
from task_design.workflow.errors import error_kind_of
from task_design.workflow.nodes import fix, generation, mock_exam, scenarios
from task_design.workflow.observer import StepPoint
from task_design.workflow.state import Deps, WorkflowState, draft_id

log = logging.getLogger(__name__)

NodeFn = Callable[[Deps, WorkflowState], dict[str, Any]]


class N:
    """Node names."""

    SEARCH_SIMILAR_TASKS = "search_similar_tasks"
    GENERATE_CANDIDATES = "generate_candidates"
    AWAIT_SELECTION = "await_selection"
    WRITE_INSTRUCTION = "write_instruction"
    WRITE_ATTACHMENTS = "write_attachments"
    WRITE_RUBRIC = "write_rubric"
    REVIEW_TASK = "review_task"
    START_MOCK_EXAM = "start_mock_exam"
    RUN_PERSONA = mock_exam.RUN_PERSONA
    START_GRADING = "start_grading"
    GRADE_SUBMISSION = mock_exam.GRADE_SUBMISSION
    JUDGE = "judge"
    FIX = "fix"
    FINISH = "finish"


# First node after the human picks a 상황 후보.
AFTER_SELECTION = N.WRITE_INSTRUCTION

# THE VERDICT EDGE: fix.route_after_verdict's return value → next node.
# "fix" regenerates one component and re-runs the 모의 응시 (자기 수정 루프).
VERDICT_ROUTES: dict[str, str] = {fix.FINISH: N.FINISH, fix.FIX: N.FIX}
# Before each round's 모의 응시: over the token budget → finish unverified.
MOCK_EXAM_ROUTES: dict[str, str] = {fix.MOCK_EXAM: N.START_MOCK_EXAM, fix.FINISH: N.FINISH}


@dataclass(frozen=True)
class NodeSpec:
    name: str
    fn: NodeFn
    stage: Stage | None = None  # shown as Draft.stage while the node runs
    # A stage boundary for the run observer (단계별 진행). False for the interrupt node
    # (it re-runs on resume) and the parallel fan-out branches (their dispatcher is
    # the boundary, so 모의 응시 and 채점 count as one step each).
    step: bool = True


def _node_specs() -> list[NodeSpec]:
    return [
        NodeSpec(N.SEARCH_SIMILAR_TASKS, scenarios.search_similar_tasks,
                 Stage.SEARCHING_SIMILAR_TASKS),
        NodeSpec(N.GENERATE_CANDIDATES, scenarios.generate_candidates,
                 Stage.GENERATING_CANDIDATES),
        NodeSpec(N.AWAIT_SELECTION, scenarios.await_selection, step=False),
        NodeSpec(N.WRITE_INSTRUCTION, generation.write_instruction, Stage.WRITING_INSTRUCTION),
        NodeSpec(N.WRITE_ATTACHMENTS, generation.write_attachments, Stage.WRITING_ATTACHMENTS),
        NodeSpec(N.WRITE_RUBRIC, generation.write_rubric, Stage.WRITING_RUBRIC),
        NodeSpec(N.REVIEW_TASK, generation.review_task, Stage.REVIEWING),
        NodeSpec(N.START_MOCK_EXAM, mock_exam.start_mock_exam, Stage.MOCK_EXAM),
        NodeSpec(N.RUN_PERSONA, mock_exam.run_persona, Stage.MOCK_EXAM, step=False),
        NodeSpec(N.START_GRADING, mock_exam.start_grading, Stage.GRADING),
        NodeSpec(N.GRADE_SUBMISSION, mock_exam.grade_submission, Stage.GRADING,
                 step=False),
        NodeSpec(N.JUDGE, mock_exam.judge, Stage.JUDGING),
        NodeSpec(N.FIX, fix.fix, Stage.FIXING),
        NodeSpec(N.FINISH, fix.finish),
    ]


def _bind(deps: Deps, spec: NodeSpec) -> Callable[[WorkflowState], dict[str, Any]]:
    def node(state: WorkflowState) -> dict[str, Any]:
        if deps.observer is not None and spec.step:  # 데모 모드: may pause here
            deps.observer.before_step(StepPoint(draft_id(state), spec.name, spec.stage, state))
        if spec.stage is not None:
            deps.drafts.set_stage(draft_id(state), spec.stage)
        with tracing.node_scope(spec.name):  # names this node's model calls in Langfuse
            return spec.fn(deps, state)

    node.__name__ = spec.name
    return node


def build_graph(deps: Deps, checkpointer: PostgresSaver | None) -> CompiledStateGraph:
    g = StateGraph(WorkflowState)
    for spec in _node_specs():
        g.add_node(spec.name, _bind(deps, spec))
    g.add_edge(START, N.SEARCH_SIMILAR_TASKS)
    g.add_edge(N.SEARCH_SIMILAR_TASKS, N.GENERATE_CANDIDATES)
    g.add_edge(N.GENERATE_CANDIDATES, N.AWAIT_SELECTION)
    g.add_conditional_edges(
        N.AWAIT_SELECTION,
        scenarios.route_after_selection,
        {"regenerate": N.GENERATE_CANDIDATES, "select": AFTER_SELECTION},
    )
    g.add_edge(N.WRITE_INSTRUCTION, N.WRITE_ATTACHMENTS)
    g.add_edge(N.WRITE_ATTACHMENTS, N.WRITE_RUBRIC)
    g.add_edge(N.WRITE_RUBRIC, N.REVIEW_TASK)
    to_mock_exam = functools.partial(fix.route_to_mock_exam, deps=deps)  # token budget
    g.add_conditional_edges(N.REVIEW_TASK, to_mock_exam, MOCK_EXAM_ROUTES)
    g.add_conditional_edges(N.START_MOCK_EXAM, mock_exam.fan_out_personas, [N.RUN_PERSONA])
    g.add_edge(N.RUN_PERSONA, N.START_GRADING)
    g.add_conditional_edges(
        N.START_GRADING,
        functools.partial(mock_exam.fan_out_gradings,
                          gradings_per_submission=deps.settings.gradings_per_submission),
        [N.GRADE_SUBMISSION],
    )
    g.add_edge(N.GRADE_SUBMISSION, N.JUDGE)
    g.add_conditional_edges(  # VERDICT EDGE
        N.JUDGE, functools.partial(fix.route_after_verdict, deps=deps), VERDICT_ROUTES)
    g.add_conditional_edges(N.FIX, to_mock_exam, MOCK_EXAM_ROUTES)
    g.add_edge(N.FINISH, END)
    return g.compile(checkpointer=checkpointer)


class Workflow:
    """Runs the graph for one draft at a time (thread id = draft id).

    ``start``/``resume``/``retry`` block until the graph pauses at an interrupt
    or ends. They never raise for a failing node: the error is stored on the
    draft (``Draft.error``) and the draft stays in its current state. They are
    safe to call from a background thread: all observable progress goes to the
    ``drafts`` table as it happens.
    """

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._graph = build_graph(deps, PostgresSaver(deps.db.autocommit_pool))

    def start(self, draft_id: UUID, input: DraftInput) -> None:
        with tracing.run_scope("start"):
            self._start(draft_id, input)

    def resume(self, draft_id: UUID, value: dict[str, Any]) -> None:
        with tracing.run_scope(value["action"]):  # "regenerate" | "select"
            self._run(draft_id, Command(resume=value))

    def retry(self, draft_id: UUID) -> None:
        """Re-run a failed run from its last checkpoint (finished parallel tasks are kept).

        Also recovers a run that never got going because the process died first: with
        no checkpoint at all it starts over from the draft's input, and when the graph
        still waits at the 상황 선택 interrupt it re-delivers what the draft's state says
        was asked for (the stored selection, or a regeneration).
        """
        with tracing.run_scope("retry"):
            self._retry(draft_id)

    def _retry(self, draft_id: UUID) -> None:
        config = {"configurable": {"thread_id": str(draft_id)}}
        snapshot = self._graph.get_state(config)
        if not snapshot.values:
            self._start(draft_id, self._deps.drafts.get(draft_id).input)
            return
        if snapshot.interrupts:
            draft = self._deps.drafts.get(draft_id)
            if draft.state is DraftState.VERIFYING and draft.selected_candidate_id:
                self._run(draft_id, Command(resume={
                    "action": "select", "candidate_id": str(draft.selected_candidate_id)}))
                return
            if draft.state is DraftState.GENERATING:
                self._run(draft_id, Command(resume={"action": "regenerate"}))
                return
        self._run(draft_id, None)

    def _start(self, draft_id: UUID, input: DraftInput) -> None:
        self._run(draft_id, {"draft_id": str(draft_id), "input": input.model_dump(mode="json")})

    def _run(self, draft_id: UUID, payload: Any) -> None:
        config = {"configurable": {"thread_id": str(draft_id)}}
        try:
            self._graph.invoke(payload, config)
        except Exception as exc:  # recorded on the draft; get_draft shows it
            log.exception("workflow run for draft %s failed", draft_id)
            self._deps.drafts.set_error(draft_id, f"{type(exc).__name__}: {exc}",
                                        error_kind_of(exc))
            return
        for pending in self._graph.get_state(config).interrupts:
            self._deps.drafts.set_state(draft_id, DraftState(pending.value["state"]))
