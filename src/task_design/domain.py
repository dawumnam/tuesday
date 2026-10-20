"""Domain types (pydantic v2). Terms: see GLOSSARY.md → "Code names".

Enum values are stable English strings (stored in the DB); ``.ko`` gives the
Korean display label.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


class _KoEnum(StrEnum):
    @property
    def ko(self) -> str:
        return _KO[self]


class DraftState(_KoEnum):
    GENERATING = "generating"
    AWAITING_SCENARIO_SELECTION = "awaiting_scenario_selection"
    VERIFYING = "verifying"
    AWAITING_REVIEW = "awaiting_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Level(_KoEnum):
    """상·중·하: target difficulty, persona level, and level-indicator level."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RubricItemType(_KoEnum):
    PROCESS = "process"  # 과정 항목: judges the AI-use transcript
    OUTPUT = "output"  # 산출물 항목: judges the final output


class AttachmentFormat(_KoEnum):
    CSV = "csv"
    MEMO = "memo"
    EMAIL = "email"
    REPORT = "report"


class Component(_KoEnum):
    SCENARIO = "scenario"
    INSTRUCTION = "instruction"
    ATTACHMENTS = "attachments"
    RUBRIC = "rubric"


# Components that receive a 검토 라벨 in final review.
LABELED_COMPONENTS = (Component.SCENARIO, Component.INSTRUCTION, Component.RUBRIC)


class ReviewLabel(_KoEnum):
    KEEP = "keep"
    PARTIAL_EDIT = "partial_edit"
    FULL_REWRITE = "full_rewrite"


class ReviewDecision(_KoEnum):
    CONFIRM = "confirm"
    REJECT = "reject"


class FailureType(_KoEnum):
    """Verdict failure types, in the order the self-correction loop handles them."""

    UNSOLVABLE = "unsolvable"
    LOW_DISCRIMINATION = "low_discrimination"
    INCONSISTENT_GRADING = "inconsistent_grading"


class Stage(_KoEnum):
    """Workflow step currently running, for progress display (``Draft.stage``).

    Later tickets append their steps here (with a ``_KO`` label).
    """

    SEARCHING_SIMILAR_TASKS = "searching_similar_tasks"
    GENERATING_CANDIDATES = "generating_candidates"
    WRITING_INSTRUCTION = "writing_instruction"
    WRITING_ATTACHMENTS = "writing_attachments"
    WRITING_RUBRIC = "writing_rubric"
    REVIEWING = "reviewing"
    MOCK_EXAM = "mock_exam"
    GRADING = "grading"
    JUDGING = "judging"
    FIXING = "fixing"  # 자기 수정: regenerating one component after a failed verdict


class PersonaStatus(_KoEnum):
    """Progress of one 응시자 페르소나 in the current round's 모의 응시 (``Draft.persona_status``)."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"


class DraftErrorKind(_KoEnum):
    """Why the draft's last workflow run failed (``Draft.error_kind``); the UI picks its
    Korean explanation by kind. Every failed run can be resumed with ``retry``."""

    INTERRUPTED = "interrupted"  # the app restarted while the run was going
    CANDIDATES_EXHAUSTED = "candidates_exhausted"  # no 3 non-duplicate 상황 후보 found
    INVALID_MODEL_OUTPUT = "invalid_model_output"  # the model kept breaking the schema/rules
    MODEL_UNAVAILABLE = "model_unavailable"  # timeout, connection or provider error
    CONFIGURATION = "configuration"  # e.g. no provider configured for a role
    UNEXPECTED = "unexpected"


class IntervalKind(_KoEnum):
    HUMAN_ACTIVE = "human_active"
    AGENT_RUN = "agent_run"


_KO: dict[StrEnum, str] = {
    DraftState.GENERATING: "생성 중",
    DraftState.AWAITING_SCENARIO_SELECTION: "상황 선택 대기",
    DraftState.VERIFYING: "검증 중",
    DraftState.AWAITING_REVIEW: "검토 대기",
    DraftState.CONFIRMED: "확정",
    DraftState.REJECTED: "반려",
    Level.HIGH: "상",
    Level.MEDIUM: "중",
    Level.LOW: "하",
    RubricItemType.PROCESS: "과정",
    RubricItemType.OUTPUT: "산출물",
    AttachmentFormat.CSV: "CSV",
    AttachmentFormat.MEMO: "메모",
    AttachmentFormat.EMAIL: "이메일",
    AttachmentFormat.REPORT: "보고서",
    Component.SCENARIO: "상황",
    Component.INSTRUCTION: "지시문",
    Component.ATTACHMENTS: "첨부 자료",
    Component.RUBRIC: "채점 기준",
    ReviewLabel.KEEP: "유지",
    ReviewLabel.PARTIAL_EDIT: "부분 수정",
    ReviewLabel.FULL_REWRITE: "전면 재작성",
    ReviewDecision.CONFIRM: "확정",
    ReviewDecision.REJECT: "반려",
    FailureType.UNSOLVABLE: "풀이 불가",
    FailureType.LOW_DISCRIMINATION: "변별력 부족",
    FailureType.INCONSISTENT_GRADING: "채점 불일치",
    Stage.SEARCHING_SIMILAR_TASKS: "유사 과제 검색",
    Stage.GENERATING_CANDIDATES: "상황 후보 생성",
    Stage.WRITING_INSTRUCTION: "지시문 작성",
    Stage.WRITING_ATTACHMENTS: "첨부 자료 작성",
    Stage.WRITING_RUBRIC: "채점 기준 작성",
    Stage.REVIEWING: "검토 에이전트 점검",
    Stage.MOCK_EXAM: "모의 응시",
    Stage.GRADING: "채점",
    Stage.JUDGING: "판정",
    Stage.FIXING: "자기 수정",
    PersonaStatus.PENDING: "대기",
    PersonaStatus.RUNNING: "응시 중",
    PersonaStatus.DONE: "제출 완료",
    DraftErrorKind.INTERRUPTED: "실행 중단",
    DraftErrorKind.CANDIDATES_EXHAUSTED: "상황 후보 부족",
    DraftErrorKind.INVALID_MODEL_OUTPUT: "모델 응답 형식 오류",
    DraftErrorKind.MODEL_UNAVAILABLE: "모델 서비스 오류",
    DraftErrorKind.CONFIGURATION: "설정 오류",
    DraftErrorKind.UNEXPECTED: "예상하지 못한 오류",
    IntervalKind.HUMAN_ACTIVE: "사람 활성",
    IntervalKind.AGENT_RUN: "에이전트 실행",
}


# --- 사람 / 입력 ---------------------------------------------------------------


class Staff(BaseModel):
    """담당자. Exactly two, seeded by migration."""

    id: int
    name: str


# --- 역량 체계 -----------------------------------------------------------------


class SubElement(BaseModel):
    """하위 요소 of a competency, with its 상·중·하 수준 지표."""

    id: str = Field(min_length=1)
    name: str
    description: str = ""
    indicators: dict[Level, str]

    @field_validator("indicators")
    @classmethod
    def _all_levels(cls, v: dict[Level, str]) -> dict[Level, str]:
        missing = [lv.value for lv in Level if lv not in v]
        if missing:
            raise ValueError(f"missing level indicators: {', '.join(missing)}")
        return v


class Competency(BaseModel):
    """역량 in the 역량 체계."""

    id: str = Field(min_length=1)
    name: str
    description: str = ""
    sub_elements: list[SubElement] = Field(default_factory=list)


class LevelIndicator(BaseModel):
    """수준 지표: one sub-element's behaviour at one level."""

    competency_id: str
    sub_element_id: str
    sub_element_name: str
    level: Level
    description: str


class Audience(BaseModel):
    """대상자: job and career level."""

    job: str = Field(min_length=1)
    career_level: str = Field(min_length=1)


class DraftInput(BaseModel):
    competency_ids: list[str] = Field(min_length=1, max_length=2)
    audience: Audience
    difficulty: Level
    memo: str | None = None

    @field_validator("competency_ids")
    @classmethod
    def _distinct(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("competency_ids must be distinct")
        return v


# --- 과제 구성 -----------------------------------------------------------------


class Trap(BaseModel):
    """함정: where in the attachment it is, and what it is."""

    location: str  # e.g. "3행 매출 열", "두 번째 문단"
    description: str


class Attachment(BaseModel):
    filename: str
    format: AttachmentFormat
    content: str
    traps: list[Trap] = Field(default_factory=list)


class RubricItem(BaseModel):
    title: str
    competency_id: str
    type: RubricItemType
    points: int = Field(gt=0)
    criteria_zero: str  # 0점 판정 기준
    criteria_partial: str  # 부분 점수 판정 기준
    criteria_full: str  # 만점 판정 기준


class Rubric(BaseModel):
    """채점 기준: 3–5 items, at least one 과정 항목."""

    items: list[RubricItem]

    @model_validator(mode="after")
    def _shape(self) -> Rubric:
        if not 3 <= len(self.items) <= 5:
            raise ValueError("rubric must have 3–5 items")
        if not any(i.type is RubricItemType.PROCESS for i in self.items):
            raise ValueError("rubric must have at least one process item")
        return self

    @property
    def total_points(self) -> int:
        return sum(i.points for i in self.items)


class Task(BaseModel):
    """과제 (also the content of a 초안). Embedding lives in the repository, not here."""

    id: UUID | None = None
    scenario: str
    instruction: str
    attachments: list[Attachment] = Field(default_factory=list)
    rubric: Rubric
    competency_ids: list[str] = Field(min_length=1, max_length=2)
    audience: Audience
    difficulty: Level


class ScenarioCandidate(BaseModel):
    """상황 후보 with the similar tasks it drew on."""

    id: UUID = Field(default_factory=uuid4)
    title: str
    scenario: str
    referenced_task_ids: list[UUID] = Field(default_factory=list)


# --- 모의 응시 / 판정 -----------------------------------------------------------


class ChatTurn(BaseModel):
    speaker: Literal["examinee", "ai_tool"]
    content: str


class ItemScore(BaseModel):
    item_index: int  # index into Rubric.items
    score: float = Field(ge=0)
    rationale: str


class Grading(BaseModel):
    """One independent grading of a submission."""

    item_scores: list[ItemScore]

    @property
    def total(self) -> float:
        return sum(s.score for s in self.item_scores)


class MockExamResult(BaseModel):
    """모의 응시 결과 for one persona: transcript + output (= 제출물) and its gradings."""

    persona_level: Level
    transcript: list[ChatTurn]
    output: str
    gradings: list[Grading]

    @property
    def mean_score(self) -> float:
        return statistics.fmean(g.total for g in self.gradings)

    @property
    def score_stdev(self) -> float:
        totals = [g.total for g in self.gradings]
        return statistics.pstdev(totals) if len(totals) > 1 else 0.0


class Verdict(BaseModel):
    solvable: bool
    discriminating: bool
    consistent: bool

    @property
    def failures(self) -> list[FailureType]:
        """Failed checks in handling order (풀이 불가 → 변별력 부족 → 채점 불일치)."""
        out = []
        if not self.solvable:
            out.append(FailureType.UNSOLVABLE)
        if not self.discriminating:
            out.append(FailureType.LOW_DISCRIMINATION)
        if not self.consistent:
            out.append(FailureType.INCONSISTENT_GRADING)
        return out

    @property
    def passed(self) -> bool:
        return not self.failures


class DraftRound(BaseModel):
    """초안 회차."""

    number: int = Field(ge=1)
    changed_components: list[Component]
    change_reason: str
    mock_exam_results: list[MockExamResult] = Field(default_factory=list)
    verdict: Verdict | None = None


class FailureReport(BaseModel):
    """실패 리포트."""

    failure_types: list[FailureType]
    scores_by_round: list[dict[Level, float]]
    attempted_fixes: list[str]
    stopped_by_token_budget: bool = False


# --- 검토 / 초안 ---------------------------------------------------------------


class Review(BaseModel):
    reviewer_id: int
    labels: dict[Component, ReviewLabel]
    decision: ReviewDecision
    edited_task: Task | None = None
    reviewed_at: datetime | None = None

    @model_validator(mode="after")
    def _all_labels(self) -> Review:
        missing = [c for c in LABELED_COMPONENTS if c not in self.labels]
        if missing:
            raise ValueError(f"missing review labels: {', '.join(m.value for m in missing)}")
        return self

    @property
    def full_rewrite(self) -> bool:
        return ReviewLabel.FULL_REWRITE in self.labels.values()


class Draft(BaseModel):
    """초안: the aggregate returned by ``TaskDesignService.get_draft``."""

    id: UUID
    author_id: int
    input: DraftInput
    state: DraftState
    stage: Stage | None = None  # step running now (None while waiting on a human / done)
    similar_tasks: list[Task] = Field(default_factory=list)  # 유사 과제 the candidates drew on
    candidates: list[ScenarioCandidate] = Field(default_factory=list)
    selected_candidate_id: UUID | None = None
    task: Task | None = None  # current content (set once 지시문·첨부 자료·채점 기준 exist)
    rounds: list[DraftRound] = Field(default_factory=list)
    # 상·중·하 persona progress in the current round's 모의 응시 (empty before the first one)
    persona_status: dict[Level, PersonaStatus] = Field(default_factory=dict)
    failure_report: FailureReport | None = None
    review: Review | None = None
    error: str | None = None  # last workflow failure (raw, for the 오류 상세), if the run crashed
    error_kind: DraftErrorKind | None = None  # its kind (set whenever ``error`` is)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DraftSummary(BaseModel):
    """One row of a 초안 목록 (``TaskDesignService.list_drafts``)."""

    id: UUID
    author_id: int
    state: DraftState
    stage: Stage | None = None
    title: str  # the chosen 상황 후보's title, else a summary of the input
    input: DraftInput
    has_error: bool = False
    created_at: datetime
    updated_at: datetime


# --- 측정 / 비용 ---------------------------------------------------------------


class Interval(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _ordered(self) -> Interval:
        if self.end < self.start:
            raise ValueError("interval end precedes start")
        return self


class MeasurementEvent(BaseModel):
    """측정 이벤트."""

    draft_id: UUID
    staff_id: int | None  # None for agent-run intervals
    kind: IntervalKind
    interval: Interval


class Cost(BaseModel):
    """비용 per draft per role."""

    draft_id: UUID
    role: str
    input_tokens: int
    output_tokens: int
    calls: int
    estimated_cost_usd: float


class DraftWorkTime(BaseModel):
    """총 작업 시간 of one reviewed draft (minutes). Human and agent time are each the
    union of that draft's intervals, so overlapping intervals count once."""

    draft_id: UUID
    decision: ReviewDecision
    passed: bool  # 확정 with no 전면 재작성 label
    reviewed_at: datetime
    human_minutes: float
    agent_minutes: float
    total_minutes: float
    trace_url: str | None  # Langfuse trace of the draft (None if not configured)


class DraftCost(BaseModel):
    """비용 of one draft over all its model calls."""

    draft_id: UUID
    created_at: datetime
    calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    trace_url: str | None


class Metrics(BaseModel):
    """Dashboard metrics for a period (see ``TaskDesignService.metrics`` for the rules)."""

    period: Interval
    # 성공 기준 1: 전면 재작성 없이 통과한 비율 (over drafts reviewed in the period)
    reviewed_drafts: int
    passed_drafts: int
    pass_without_full_rewrite_ratio: float | None  # None when nothing was reviewed
    pass_ratio_target: float
    meets_pass_ratio_target: bool | None
    # 성공 기준 2: 과제 1개당 총 작업 시간 (same drafts)
    mean_total_work_minutes: float | None
    work_minutes_target: float
    meets_work_minutes_target: bool | None
    work_times: list[DraftWorkTime]  # one per reviewed draft, by reviewed_at
    label_distribution: dict[Component, dict[ReviewLabel, int]]
    # 비용: drafts created in the period
    costs: list[Cost]  # per draft per role
    draft_costs: list[DraftCost]  # per draft, by created_at
    total_calls: int
    total_cost_usd: float
