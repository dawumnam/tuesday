"""과제 설계 서비스: the single entry point for the UI and the test boundary."""

from __future__ import annotations

from uuid import UUID

from task_design.db import Database
from task_design.domain import (
    Component,
    Draft,
    DraftInput,
    Interval,
    Metrics,
    ReviewDecision,
    ReviewLabel,
    Staff,
    Task,
)
from task_design.gateway import ModelGateway
from task_design.settings import Settings


class TaskDesignService:
    def __init__(self, db: Database, gateway: ModelGateway, settings: Settings) -> None:
        self.db = db
        self.gateway = gateway
        self.settings = settings

    def list_staff(self) -> list[Staff]:
        """The two fixed 담당자 (for the top-of-screen selector)."""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT id, name FROM staff ORDER BY id").fetchall()
        return [Staff(id=r[0], name=r[1]) for r in rows]

    def start(self, input: DraftInput, author_id: int) -> Draft:
        """Create a draft with 3 상황 후보; state → 상황 선택 대기. (ticket 05)"""
        raise NotImplementedError

    def regenerate_scenarios(self, draft_id: UUID) -> Draft:
        """Replace the 상황 후보; only in 상황 선택 대기. (ticket 05)"""
        raise NotImplementedError

    def get_draft(self, draft_id: UUID) -> Draft:
        """Current state, stage, candidates, rounds, mock exam results. (ticket 05)"""
        raise NotImplementedError

    def select_scenario(self, draft_id: UUID, candidate_id: UUID) -> Draft:
        """Generate, mock-exam and self-correct; state → 검토 대기. (tickets 06, 07)"""
        raise NotImplementedError

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
