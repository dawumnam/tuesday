"""과제 설계 서비스: the single entry point for the UI and the test boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from task_design.db import Database
from task_design.domain import (
    Component,
    Draft,
    DraftInput,
    DraftState,
    Interval,
    Metrics,
    ReviewDecision,
    ReviewLabel,
    Staff,
    Task,
)
from task_design.gateway import ModelGateway
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.settings import Settings
from task_design.workflow import Deps, DraftStore, Workflow

# Runs one unit of graph work. Default: inline (blocking). The UI (ticket 10)
# passes e.g. ``executor.submit`` so start/regenerate return immediately and
# progress is observed by polling ``get_draft``.
GraphRunner = Callable[[Callable[[], None]], Any]


def _run_inline(job: Callable[[], None]) -> None:
    job()


class UnknownCompetency(ValueError):
    """A competency id that is not in the 역량 체계."""


class TaskDesignService:
    def __init__(
        self,
        db: Database,
        gateway: ModelGateway,
        settings: Settings,
        *,
        run_graph: GraphRunner = _run_inline,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.settings = settings
        self._run_graph = run_graph
        self._drafts = DraftStore(db)
        self._competencies = CompetencyRepository(db)
        self._workflow = Workflow(
            Deps(
                db=db,
                gateway=gateway,
                settings=settings,
                drafts=self._drafts,
                tasks=TaskRepository(db, gateway, settings),
                competencies=self._competencies,
            )
        )

    def list_staff(self) -> list[Staff]:
        """The two fixed 담당자 (for the top-of-screen selector)."""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT id, name FROM staff ORDER BY id").fetchall()
        return [Staff(id=r[0], name=r[1]) for r in rows]

    def start(self, input: DraftInput | dict[str, Any], author_id: int) -> Draft:
        """Create a draft and generate 3 상황 후보; state 생성 중 → 상황 선택 대기.

        Raises ``pydantic.ValidationError`` for invalid input (e.g. not 1–2 역량) and
        ``UnknownCompetency`` for a competency id not in the 역량 체계.
        A failing workflow step does not raise: the draft is returned with ``error`` set.
        """
        input = DraftInput.model_validate(
            input.model_dump() if isinstance(input, DraftInput) else input
        )
        known = {c.id for c in self._competencies.list()}
        unknown = [cid for cid in input.competency_ids if cid not in known]
        if unknown:
            raise UnknownCompetency(f"not in the 역량 체계: {', '.join(unknown)}")
        draft_id = uuid4()
        self._drafts.create(draft_id, author_id, input)
        self._run_graph(lambda: self._workflow.start(draft_id, input))
        return self.get_draft(draft_id)

    def regenerate_scenarios(self, draft_id: UUID) -> Draft:
        """Replace the 상황 후보 with 3 new ones; state 상황 선택 대기 → 생성 중 → 상황 선택 대기.

        Raises ``InvalidDraftState`` unless the draft is in 상황 선택 대기.
        """
        self._drafts.transition(
            draft_id, DraftState.AWAITING_SCENARIO_SELECTION, DraftState.GENERATING
        )
        self._run_graph(lambda: self._workflow.resume(draft_id, {"action": "regenerate"}))
        return self.get_draft(draft_id)

    def get_draft(self, draft_id: UUID) -> Draft:
        """Current state, stage, candidates, rounds, mock exam results.

        Reads only the drafts table, so it is cheap to poll while a run is going.
        Raises ``DraftNotFound``.
        """
        return self._drafts.get(draft_id)

    def select_scenario(self, draft_id: UUID, candidate_id: UUID) -> Draft:
        """Generate, mock-exam and self-correct; state 검증 중 → 검토 대기. (tickets 06, 07)

        Raises ``InvalidDraftState`` unless the draft is in 상황 선택 대기, and
        ``UnknownCandidate`` unless ``candidate_id`` is one of its current 상황 후보.
        """
        self._drafts.select_candidate(draft_id, candidate_id)
        self._run_graph(
            lambda: self._workflow.resume(
                draft_id, {"action": "select", "candidate_id": str(candidate_id)}
            )
        )
        return self.get_draft(draft_id)

    def retry(self, draft_id: UUID) -> Draft:
        """Re-run a draft whose workflow run failed (``Draft.error`` set), from its last
        checkpoint; work that finished before the failure is not redone.

        Raises ``InvalidDraftState`` if the draft's last run did not fail.
        """
        self._drafts.claim_retry(draft_id)
        self._run_graph(lambda: self._workflow.retry(draft_id))
        return self.get_draft(draft_id)

    def review(
        self,
        draft_id: UUID,
        reviewer_id: int,
        labels: dict[Component, ReviewLabel],
        decision: ReviewDecision,
        edited_task: Task | None = None,
    ) -> Draft:
        """최종 검토: confirm (into the task DB) or reject. (ticket 08)"""
        raise NotImplementedError

    def metrics(self, period: Interval) -> Metrics:
        """Dashboard metrics over ``period``. (ticket 09)"""
        raise NotImplementedError

    def record_activity(self, draft_id: UUID, staff_id: int, interval: Interval) -> None:
        """Record one human-active interval. (ticket 09)"""
        raise NotImplementedError
