"""Optional run observer: a hook at every stage boundary, plus search/duplicate traces.

Real mode installs none (``Deps.observer is None``): nodes then skip all of this
and run exactly as without it. The 데모 모드 installs ``task_design.stepper.Stepper``
to pause before each stage (단계별 진행) and to show the RAG intermediates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from task_design.domain import Stage
from task_design.traces import DuplicateCheckTrace, SearchTrace


@dataclass(frozen=True)
class StepPoint:
    """A stage boundary: the run is about to enter ``node``.

    ``stage`` is what ``Draft.stage`` will show (``None`` for 마무리, the hand-over to
    검토 대기). ``state`` is the graph state at that point (JSON-plain, read-only): it
    holds everything the earlier stages produced.
    """

    draft_id: UUID
    node: str
    stage: Stage | None
    state: Mapping[str, Any]


class RunObserver(Protocol):
    def before_step(self, point: StepPoint) -> None:
        """Called before every stage (not per parallel branch). May block to pause the
        run; nothing holds a DB connection or transaction while it does."""

    def record_trace(self, draft_id: UUID, trace: SearchTrace | DuplicateCheckTrace) -> None:
        """How 유사 과제 검색 / 중복 검사 reached their result (only built when observed)."""
