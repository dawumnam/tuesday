"""Structured-output schemas the workflow asks the model gateway for.

These are LLM response shapes, not domain types; nodes convert them into
``task_design.domain`` objects.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from task_design.domain import Attachment, ItemScore, Rubric, RubricItem


class CandidateProposal(BaseModel):
    title: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    # Ids (as listed in the prompt) of the 유사 과제 this candidate drew on.
    referenced_task_ids: list[str] = Field(default_factory=list)


class CandidateBatch(BaseModel):
    """One generation call's 상황 후보 (normally 3; the node takes what it needs)."""

    candidates: list[CandidateProposal]


class InstructionDraft(BaseModel):
    """지시문: what the examinee must do and submit."""

    instruction: str = Field(min_length=1)


class AttachmentBatch(BaseModel):
    """첨부 자료 for the task, each with its 함정 marked."""

    attachments: list[Attachment] = Field(min_length=1)

    @field_validator("attachments")
    @classmethod
    def _traps_marked(cls, v: list[Attachment]) -> list[Attachment]:
        if not any(a.traps for a in v):
            raise ValueError("at least one attachment must contain a marked trap")
        return v


class RubricDraft(BaseModel):
    """채점 기준 (shape checked by ``Rubric``: 3–5 items, ≥1 과정 항목)."""

    items: list[RubricItem]

    def to_rubric(self) -> Rubric:
        return Rubric(items=self.items)

    @model_validator(mode="after")
    def _shape(self) -> RubricDraft:
        self.to_rubric()
        return self


class ReviewFindings(BaseModel):
    """검토 에이전트: is the 지시문 consistent with the 채점 기준? If not, which one to rewrite."""

    consistent: bool
    target: Literal["instruction", "rubric"] | None = None  # the part to regenerate
    issues: list[str] = Field(default_factory=list)


class ExamineeAction(BaseModel):
    """One persona move: ask the AI 도구 LLM something, or submit the final output."""

    action: Literal["ask", "submit"]
    content: str = Field(min_length=1)


class GradingDraft(BaseModel):
    """One grading: a score and rationale per 채점 기준 항목 (item_index = position, 0-based)."""

    item_scores: list[ItemScore]
