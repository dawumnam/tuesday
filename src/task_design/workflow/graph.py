"""The LangGraph state graph and its runner.

Graph (ticket 05)::

    START → search_similar_tasks → generate_candidates → await_selection ─┬─ regenerate → generate_candidates
                                                                          └─ select     → AFTER_SELECTION

To extend (ticket 06+): add ``NodeSpec`` entries to ``_node_specs``, wire them
in ``build_graph``, and point ``AFTER_SELECTION`` at the first new node.
Each node is ``fn(deps, state) -> partial state``; when the spec has a
``stage``, the draft's ``stage`` column is set before the node runs.
"""

from __future__ import annotations

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
from task_design.workflow.nodes import scenarios
from task_design.workflow.state import Deps, WorkflowState, draft_id

log = logging.getLogger(__name__)

NodeFn = Callable[[Deps, WorkflowState], dict[str, Any]]


class N:
    """Node names."""

    SEARCH_SIMILAR_TASKS = "search_similar_tasks"
    GENERATE_CANDIDATES = "generate_candidates"
    AWAIT_SELECTION = "await_selection"


# First node after the human picks a 상황 후보. Ticket 06 replaces END.
AFTER_SELECTION = END


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
    return g.compile(checkpointer=checkpointer)


class Workflow:
    """Runs the graph for one draft at a time (thread id = draft id).

    ``start``/``resume`` block until the graph pauses at an interrupt or ends.
    They never raise for a failing node: the error is stored on the draft
    (``Draft.error``) and the draft stays in its current state. They are safe
    to call from a background thread: all observable progress goes to the
    ``drafts`` table as it happens.
    """

    def __init__(self, deps: Deps) -> None:
        self._deps = deps
        self._graph = build_graph(deps, PostgresSaver(deps.db.autocommit_pool))

    def start(self, draft_id: UUID, input: DraftInput) -> None:
        self._run(draft_id, {"draft_id": str(draft_id), "input": input.model_dump(mode="json")})

    def resume(self, draft_id: UUID, value: dict[str, Any]) -> None:
        self._run(draft_id, Command(resume=value))

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
