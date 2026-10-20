"""Structured-output schemas the workflow asks the model gateway for.

These are LLM response shapes, not domain types; nodes convert them into
``task_design.domain`` objects.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateProposal(BaseModel):
    title: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    # Ids (as listed in the prompt) of the 유사 과제 this candidate drew on.
    referenced_task_ids: list[str] = Field(default_factory=list)


class CandidateBatch(BaseModel):
    """One generation call's 상황 후보 (normally 3; the node takes what it needs)."""

    candidates: list[CandidateProposal]
