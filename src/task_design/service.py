"""과제 설계 서비스: the single entry point for the UI and the test boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import ValidationError

from task_design.db import Database
from task_design.domain import (
    LABELED_COMPONENTS,
    Competency,
    Component,
    Draft,
    DraftInput,
    DraftState,
    DraftSummary,
    Interval,
    IntervalKind,
    Metrics,
    Review,
    ReviewDecision,
    ReviewLabel,
    Staff,
    Task,
)
from task_design.gateway import ModelGateway
from task_design.measurement import MeasurementStore, compute_metrics
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.settings import Settings
from task_design.workflow import (
    Deps,
    DraftNotFound,
    DraftStore,
    InvalidDraftState,
    UnknownCandidate,
    Workflow,
)
from task_design.workflow.observer import RunObserver

# Every exception the service raises (messages are Korean, meant for the user).
__all__ = [
    "INTERRUPTED_RUN_ERROR",
    "DraftNotFound",
    "InvalidActivityInterval",
    "InvalidDraftInput",
    "InvalidDraftState",
    "InvalidEditedTask",
    "InvalidPeriod",
    "InvalidReviewLabels",
    "SelfReview",
    "TaskDesignService",
    "UnknownCandidate",
    "UnknownCompetency",
    "UnknownStaff",
]

# Runs one unit of graph work. Default: inline (blocking). The UI (ticket 10)
# passes e.g. ``executor.submit`` so start/regenerate return immediately and
# progress is observed by polling ``get_draft``.
GraphRunner = Callable[[Callable[[], None]], Any]


log = logging.getLogger(__name__)


def _run_inline(job: Callable[[], None]) -> None:
    job()


# Draft.error of a run cut off by a restart (see ``recover_interrupted_runs``).
INTERRUPTED_RUN_ERROR = "앱이 다시 시작되어 진행 중이던 실행이 중단되었습니다."


class InvalidDraftInput(ValueError):
    """The 초안 input breaks a rule (1–2 distinct 역량, 대상자 직무·경력 수준, 난이도).

    The message is Korean and lists every problem, for the UI and the CLI.
    """


_INPUT_PROBLEMS = {
    "competency_ids": "역량은 서로 다른 것으로 1~2개 골라야 합니다",
    "job": "대상자 직무를 입력해야 합니다",
    "career_level": "대상자 경력 수준을 입력해야 합니다",
    "difficulty": "목표 난이도는 상·중·하 중 하나여야 합니다",
    "memo": "메모는 글자여야 합니다",
}


def _check_input(input: DraftInput | dict[str, Any]) -> DraftInput:
    raw = input.model_dump() if isinstance(input, DraftInput) else input
    problems: list[str] = []
    try:
        parsed = DraftInput.model_validate(raw)
    except ValidationError as e:
        parsed = None
        for err in e.errors():
            field = next((str(p) for p in reversed(err["loc"]) if str(p) in _INPUT_PROBLEMS),
                         None)
            text = _INPUT_PROBLEMS.get(field or "", "입력 형식이 올바르지 않습니다")
            if field is None and err["loc"] and err["loc"][0] == "audience":
                text = "대상자 직무와 경력 수준을 입력해야 합니다"
            problems.append(text)
    if parsed is not None:
        if not parsed.audience.job.strip():
            problems.append(_INPUT_PROBLEMS["job"])
        if not parsed.audience.career_level.strip():
            problems.append(_INPUT_PROBLEMS["career_level"])
    if problems:
        raise InvalidDraftInput("입력을 확인하세요: " + "; ".join(dict.fromkeys(problems)) + ".")
    assert parsed is not None
    return parsed


class UnknownCompetency(ValueError):
    """A competency id that is not in the 역량 체계. (Message is Korean, shown in the UI.)"""


class SelfReview(PermissionError):
    """The reviewer is the draft's author (교차 검토 원칙)."""


class InvalidReviewLabels(ValueError):
    """검토 라벨 missing for 상황·지시문·채점 기준, or given for another component."""


class InvalidEditedTask(ValueError):
    """The reviewer's 수정본 breaks a domain rule."""


class UnknownStaff(LookupError):
    """No 담당자 with that id."""


class InvalidActivityInterval(ValueError):
    """A 사람 활성 구간 that is malformed, ends before it starts, or has no time zone."""


class InvalidPeriod(ValueError):
    """The metrics period is malformed (missing bounds, end before start, no time zone)."""


def _interval(value: Interval | dict[str, Any], error: type[ValueError], what: str) -> Interval:
    try:
        interval = Interval.model_validate(
            value.model_dump() if isinstance(value, Interval) else value
        )
    except ValidationError as e:
        raise error(f"{what}이 올바르지 않습니다. 시작과 끝을 주고, 끝은 시작보다 늦어야 합니다.") from e
    if interval.start.tzinfo is None or interval.end.tzinfo is None:
        raise error(f"{what}의 시작과 끝에는 시간대가 있어야 합니다.")
    return interval


class TaskDesignService:
    def __init__(
        self,
        db: Database,
        gateway: ModelGateway,
        settings: Settings,
        *,
        run_graph: GraphRunner = _run_inline,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        observer: RunObserver | None = None,
    ) -> None:
        """``observer`` is for the 데모 모드 only (단계별 진행 and RAG traces, see
        ``task_design.stepper``); leave it ``None`` for real runs."""
        self.db = db
        self.gateway = gateway
        self.settings = settings
        self._run_graph = run_graph
        self._clock = clock
        self._measurements = MeasurementStore(db)
        self._drafts = DraftStore(db)
        self._competencies = CompetencyRepository(db)
        self._tasks = TaskRepository(db, gateway, settings)
        self._workflow = Workflow(
            Deps(
                db=db,
                gateway=gateway,
                settings=settings,
                drafts=self._drafts,
                tasks=self._tasks,
                competencies=self._competencies,
                observer=observer,
            )
        )

    def list_staff(self) -> list[Staff]:
        """The two fixed 담당자 (for the top-of-screen selector)."""
        with self.db.connection() as conn:
            rows = conn.execute("SELECT id, name FROM staff ORDER BY id").fetchall()
        return [Staff(id=r[0], name=r[1]) for r in rows]

    def list_competencies(self) -> list[Competency]:
        """The 역량 체계 (for the 입력 폼), ordered by id."""
        return self._competencies.list()

    def list_drafts(
        self, *, author_id: int | None = None, states: list[DraftState] | None = None
    ) -> list[DraftSummary]:
        """초안 목록, newest first: e.g. one 담당자's drafts, or the 검토 대기 queue."""
        return self._drafts.list(author_id=author_id, states=states)

    def start(self, input: DraftInput | dict[str, Any], author_id: int) -> Draft:
        """Create a draft and generate 3 상황 후보; state 생성 중 → 상황 선택 대기.

        Raises ``InvalidDraftInput`` (Korean message) for invalid input (e.g. not 1–2
        역량, blank 직무) and ``UnknownCompetency`` for a competency id not in the 역량 체계.
        A failing workflow step does not raise: the draft is returned with ``error`` set.
        """
        input = _check_input(input)
        known = {c.id for c in self._competencies.list()}
        unknown = [cid for cid in input.competency_ids if cid not in known]
        if unknown:
            raise UnknownCompetency(f"역량 체계에 없는 역량입니다: {', '.join(unknown)}")
        draft_id = uuid4()
        self._drafts.create(draft_id, author_id, input)
        self._execute(draft_id, lambda: self._workflow.start(draft_id, input))
        return self.get_draft(draft_id)

    def regenerate_scenarios(self, draft_id: UUID) -> Draft:
        """Replace the 상황 후보 with 3 new ones; state 상황 선택 대기 → 생성 중 → 상황 선택 대기.

        Raises ``InvalidDraftState`` unless the draft is in 상황 선택 대기.
        """
        self._drafts.transition(
            draft_id, DraftState.AWAITING_SCENARIO_SELECTION, DraftState.GENERATING
        )
        self._execute(draft_id, lambda: self._workflow.resume(draft_id, {"action": "regenerate"}))
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
        self._execute(
            draft_id,
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
        self._execute(draft_id, lambda: self._workflow.retry(draft_id))
        return self.get_draft(draft_id)

    def recover_interrupted_runs(self) -> list[UUID]:
        """Mark runs cut off by a process restart as failed, so ``retry`` can resume them.

        Call once at app start-up, before any run is handed to ``run_graph``: every
        draft still in 생성 중 / 검증 중 without an error can only be one whose
        background job died with the previous process. Returns their ids.
        """
        return self._drafts.mark_interrupted(INTERRUPTED_RUN_ERROR)

    def review(
        self,
        draft_id: UUID,
        reviewer_id: int,
        labels: dict[Component, ReviewLabel],
        decision: ReviewDecision,
        edited_task: Task | dict[str, Any] | None = None,
    ) -> Draft:
        """최종 검토: state 검토 대기 → 확정 | 반려, storing the review and its 검토 라벨.

        ``labels`` must hold exactly 상황·지시문·채점 기준. ``edited_task`` is the
        reviewer's 수정본 (default: the draft's task as the agent left it). On 확정 the
        수정본 is validated and the task (with edits) goes into the 과제 DB under the
        draft's id, so it is found by 유사 과제 검색. On 반려 the draft is kept with its
        labels, and the 수정본 (if any) is stored as given, without validation.

        Raises ``DraftNotFound``; ``SelfReview`` if the reviewer wrote the draft;
        ``InvalidReviewLabels``; ``InvalidEditedTask`` if a 확정 수정본 breaks a domain
        rule (100점 배점 etc.); ``InvalidDraftState`` unless the draft is in 검토 대기.
        """
        draft = self.get_draft(draft_id)
        if reviewer_id == draft.author_id:
            raise SelfReview("자신이 작성한 초안은 최종 검토할 수 없습니다. 다른 담당자가 검토해야 합니다.")
        labels = self._check_labels(labels)
        decision = ReviewDecision(decision)
        edited: Task | dict[str, Any] | None
        if edited_task is None:
            edited = None
        elif decision is ReviewDecision.CONFIRM:
            edited = self._check_edited_task(draft, edited_task)
        else:  # 반려: kept as given, for failure analysis
            edited = edited_task if isinstance(edited_task, Task) else dict(edited_task)
        review = Review(reviewer_id=reviewer_id, labels=labels, decision=decision,
                        edited_task=edited)

        def confirm() -> UUID:
            assert edited is None or isinstance(edited, Task)
            task = (edited or draft.task).model_copy(update={"id": draft_id})
            return self._tasks.add(task, draft_id=draft_id)

        self._drafts.save_review(
            draft_id, review, confirm=confirm if decision is ReviewDecision.CONFIRM else None
        )
        return self.get_draft(draft_id)

    @staticmethod
    def _check_labels(labels: dict[Any, Any]) -> dict[Component, ReviewLabel]:
        try:
            parsed = {Component(c): ReviewLabel(v) for c, v in labels.items()}
        except ValueError:
            parsed = None
        if parsed is None or set(parsed) != set(LABELED_COMPONENTS):
            names = "·".join(c.ko for c in LABELED_COMPONENTS)
            raise InvalidReviewLabels(f"{names} 각각에 검토 라벨을 하나씩 붙여야 합니다.")
        return parsed

    @staticmethod
    def _check_edited_task(draft: Draft, edited: Task | dict[str, Any]) -> Task:
        try:
            task = Task.model_validate(
                edited.model_dump() if isinstance(edited, Task) else edited
            )
        except ValidationError as e:
            raise InvalidEditedTask(
                "수정본의 형식이 올바르지 않습니다. 채점 기준은 항목 3~5개에 과정 항목이 1개 이상이어야 "
                "하고, 상황·지시문·연결 역량이 있어야 합니다."
            ) from e
        problems = []
        if not task.scenario.strip() or not task.instruction.strip():
            problems.append("상황과 지시문은 비워 둘 수 없습니다")
        if set(task.competency_ids) - set(draft.input.competency_ids):
            problems.append(f"연결 역량은 초안의 역량({', '.join(draft.input.competency_ids)}) "
                            "중에서만 고를 수 있습니다")
        if {i.competency_id for i in task.rubric.items} - set(task.competency_ids):
            problems.append("채점 기준 항목의 역량은 연결 역량 중 하나여야 합니다")
        if task.rubric.total_points != 100:
            problems.append(f"배점 합계가 100점이어야 합니다 (현재 {task.rubric.total_points}점)")
        if problems:
            raise InvalidEditedTask("수정본을 확정할 수 없습니다: " + "; ".join(problems) + ".")
        return task

    def metrics(self, period: Interval | dict[str, Any]) -> Metrics:
        """Dashboard metrics over ``period`` (start inclusive, end exclusive).

        - 전면 재작성 없이 통과한 비율 = drafts 확정 with no 전면 재작성 label on any
          component / all drafts reviewed (확정 + 반려); 반려 counts as a failure.
        - Review metrics (ratio, 총 작업 시간, label distribution) cover drafts whose
          review (``reviewed_at``) is in the period; unreviewed drafts are left out.
        - 총 작업 시간 per draft = union of its 사람 활성 intervals + union of its
          에이전트 실행 intervals (overlaps are not double counted, gaps not counted).
        - 비용 covers every draft created in the period, with all its model calls.
        - Targets (70%, 42분) come from ``settings.pass_ratio_target`` /
          ``settings.work_minutes_target``; trace links need ``langfuse_project_id``.

        Raises ``InvalidPeriod`` for a malformed period.
        """
        return compute_metrics(self.db, self.settings, _interval(period, InvalidPeriod,
                                                               "기간"))

    def record_activity(
        self, draft_id: UUID, staff_id: int, interval: Interval | dict[str, Any]
    ) -> None:
        """Record one 사람 활성 구간 of ``staff_id`` working on ``draft_id``.

        The UI sends only intervals in which the tab was visible and the person active.
        Intervals may overlap or repeat (e.g. heartbeats, two tabs): metrics takes the
        union per draft, so nothing is double counted. Any draft state is accepted.

        Raises ``DraftNotFound``, ``UnknownStaff``, and ``InvalidActivityInterval``
        (malformed, end before start, or no time zone).
        """
        interval = _interval(interval, InvalidActivityInterval, "활성 구간")
        self.get_draft(draft_id)
        with self.db.connection() as conn:
            known = conn.execute("SELECT 1 FROM staff WHERE id = %s", (staff_id,)).fetchone()
        if known is None:
            raise UnknownStaff("등록되지 않은 담당자입니다.")
        self._measurements.record(draft_id, staff_id, IntervalKind.HUMAN_ACTIVE, interval)

    def _execute(self, draft_id: UUID, run: Callable[[], None]) -> None:
        """Hand one graph execution to ``run_graph``, recording it as an 에이전트 실행 구간.

        Every graph execution (start, regenerate, select_scenario incl. its fix rounds,
        retry) goes through here; the interval is measured where the job actually runs,
        inline or in a background thread, and is recorded even if the run fails.
        """

        def job() -> None:
            started = self._clock()
            try:
                run()
            finally:
                ended = self._clock()
                try:
                    self._measurements.record(
                        draft_id, None, IntervalKind.AGENT_RUN,
                        Interval(start=started, end=max(started, ended)),
                    )
                except Exception:  # measurement must never break a run
                    log.exception("could not record agent run of draft %s", draft_id)

        self._run_graph(job)
