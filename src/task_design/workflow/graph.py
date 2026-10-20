"""The LangGraph state graph and its runner.

Graph::

    START → search_similar_tasks → generate_candidates → await_selection ─┬─ regenerate → generate_candidates
                                                                          └─ select → write_instruction
    write_instruction → write_attachments → write_rubric → review_task
      → start_mock_exam ══Send×3══▶ run_persona (상·중·하 in parallel)
      → start_grading   ══Send×9══▶ grade_submission (3 per submission, in parallel)
      → judge ── VERDICT EDGE (route_after_verdict / VERDICT_ROUTES) ──▶ finish → END

The verdict edge is the one place ticket 07 reroutes: make
``mock_exam.route_after_verdict`` return "fix" while the loop may continue and
add ``"fix": N.<fix node>`` to ``VERDICT_ROUTES``. A fix node updates ``task``,
bumps ``round``, sets ``round_changes``, and edges back to
``N.START_MOCK_EXAM``.

Each node is ``fn(deps, state) -> partial state``; when the spec has a
``stage``, the draft's ``stage`` column is set before the node runs.
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

from task_design.domain import DraftInput, DraftState, Stage
from task_design.workflow.nodes import generation, mock_exam, scenarios
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
    FINISH = "finish"


# First node after the human picks a 상황 후보.
AFTER_SELECTION = N.WRITE_INSTRUCTION

# THE VERDICT EDGE: route_after_verdict's return value → next node.
# Ticket 06 always finishes (검토 대기). Ticket 07 adds the fix loop here.
VERDICT_ROUTES: dict[str, str] = {"finish": N.FINISH}


@dataclass(frozen=True)
class NodeSpec:
    name: str
    fn: NodeFn
    stage: Stage | None = None  # shown as Draft.stage while the node runs


def _node_specs() -> list[NodeSpec]:
    return [
        NodeSpec(N.SEARCH_SIMILAR_TASKS, scenarios.search_similar_tasks,
                 Stage.SEARCHING_SIMILAR_TASKS),
        NodeSpec(N.GENERATE_CANDIDATES, scenarios.generate_candidates,
                 Stage.GENERATING_CANDIDATES),
        NodeSpec(N.AWAIT_SELECTION, scenarios.await_selection),
        NodeSpec(N.WRITE_INSTRUCTION, generation.write_instruction, Stage.WRITING_INSTRUCTION),
        NodeSpec(N.WRITE_ATTACHMENTS, generation.write_attachments, Stage.WRITING_ATTACHMENTS),
        NodeSpec(N.WRITE_RUBRIC, generation.write_rubric, Stage.WRITING_RUBRIC),
        NodeSpec(N.REVIEW_TASK, generation.review_task, Stage.REVIEWING),
        NodeSpec(N.START_MOCK_EXAM, mock_exam.start_mock_exam, Stage.MOCK_EXAM),
        NodeSpec(N.RUN_PERSONA, mock_exam.run_persona, Stage.MOCK_EXAM),
        NodeSpec(N.START_GRADING, mock_exam.start_grading, Stage.GRADING),
        NodeSpec(N.GRADE_SUBMISSION, mock_exam.grade_submission, Stage.GRADING),
        NodeSpec(N.JUDGE, mock_exam.judge, Stage.JUDGING),
        NodeSpec(N.FINISH, mock_exam.finish),
    ]


def _bind(deps: Deps, spec: NodeSpec) -> Callable[[WorkflowState], dict[str, Any]]:
    def node(state: WorkflowState) -> dict[str, Any]:
        if spec.stage is not None:
            deps.drafts.set_stage(draft_id(state), spec.stage)
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
    g.add_edge(N.REVIEW_TASK, N.START_MOCK_EXAM)
    g.add_conditional_edges(N.START_MOCK_EXAM, mock_exam.fan_out_personas, [N.RUN_PERSONA])
    g.add_edge(N.RUN_PERSONA, N.START_GRADING)
    g.add_conditional_edges(
        N.START_GRADING,
        functools.partial(mock_exam.fan_out_gradings,
                          gradings_per_submission=deps.settings.gradings_per_submission),
        [N.GRADE_SUBMISSION],
    )
    g.add_edge(N.GRADE_SUBMISSION, N.JUDGE)
    g.add_conditional_edges(N.JUDGE, mock_exam.route_after_verdict, VERDICT_ROUTES)  # VERDICT EDGE
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
        self._run(draft_id, {"draft_id": str(draft_id), "input": input.model_dump(mode="json")})

    def resume(self, draft_id: UUID, value: dict[str, Any]) -> None:
        self._run(draft_id, Command(resume=value))

    def retry(self, draft_id: UUID) -> None:
        """Re-run a failed run from its last checkpoint (finished parallel tasks are kept)."""
        self._run(draft_id, None)

    def _run(self, draft_id: UUID, payload: Any) -> None:
        config = {"configurable": {"thread_id": str(draft_id)}}
        try:
            self._graph.invoke(payload, config)
        except Exception as exc:  # recorded on the draft; get_draft shows it
            log.exception("workflow run for draft %s failed", draft_id)
            self._deps.drafts.set_error(draft_id, f"{type(exc).__name__}: {exc}")
            return
        for pending in self._graph.get_state(config).interrupts:
            self._deps.drafts.set_state(draft_id, DraftState(pending.value["state"]))
