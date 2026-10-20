"""데모 모드 단계별 진행: pause a draft's run before each workflow stage.

``Stepper`` is the run observer the demo runtime installs
(``TaskDesignService(..., observer=Stepper(...))``). Per draft it has a mode:

- 단계별 진행 (``StepMode.STEP``): the run waits before every stage until
  ``step`` (다음 단계) releases exactly that one stage.
- 자동 진행 (``StepMode.AUTO``): the run goes on by itself; ``pause`` (일시정지)
  makes it wait at the next stage boundary, ``resume`` (계속) lets it go on. A
  paused run can also be advanced one stage with ``step``.

It also keeps the latest 유사 과제 검색 / 중복 검사 traces per draft for the screen.
Everything is in memory (one app process); it is lost on restart, and a paused
run occupies one background worker thread while it waits.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from task_design.traces import DuplicateCheckTrace, SearchTrace
from task_design.workflow.observer import StepPoint


class StepMode(StrEnum):
    STEP = "step"  # 단계별 진행
    AUTO = "auto"  # 자동 진행

    @property
    def ko(self) -> str:
        return "단계별 진행" if self is StepMode.STEP else "자동 진행"


@dataclass(frozen=True)
class StepStatus:
    mode: StepMode
    paused: bool  # 자동 진행 only: 일시정지 was pressed
    waiting: StepPoint | None  # the stage the run is held before, if any
    previous: StepPoint | None  # the stage released before that (what just ran)


@dataclass
class _Run:
    mode: StepMode
    paused: bool = False
    waiting: StepPoint | None = None
    previous: StepPoint | None = None
    released: int = 0  # bumped by step(): lets the waiting point through
    traces: dict[str, SearchTrace | DuplicateCheckTrace] = field(default_factory=dict)


class Stepper:
    def __init__(self, default_mode: StepMode | str = StepMode.STEP) -> None:
        self.default_mode = StepMode(default_mode)
        self._runs: dict[UUID, _Run] = {}
        self._cond = threading.Condition()

    def _run(self, draft_id: UUID) -> _Run:
        run = self._runs.get(draft_id)
        if run is None:
            run = self._runs[draft_id] = _Run(self.default_mode)
        return run

    # --- RunObserver -------------------------------------------------------------

    def before_step(self, point: StepPoint) -> None:
        with self._cond:
            run = self._run(point.draft_id)
            ticket = run.released
            if not self._holds(run):
                run.previous = point
                return
            run.waiting = point
            self._cond.notify_all()
            self._cond.wait_for(lambda: run.released != ticket or not self._holds(run))
            if run.waiting is point:
                run.waiting = None
                run.previous = point
            self._cond.notify_all()

    def record_trace(self, draft_id: UUID, trace: SearchTrace | DuplicateCheckTrace) -> None:
        with self._cond:
            self._run(draft_id).traces[trace.kind] = trace

    # --- controls ----------------------------------------------------------------

    @staticmethod
    def _holds(run: _Run) -> bool:
        return run.mode is StepMode.STEP or run.paused

    def status(self, draft_id: UUID) -> StepStatus:
        with self._cond:
            run = self._run(draft_id)
            return StepStatus(run.mode, run.paused, run.waiting, run.previous)

    def step(self, draft_id: UUID) -> None:
        """다음 단계: release the stage the run waits before (no-op if it isn't waiting)."""
        with self._cond:
            run = self._run(draft_id)
            if run.waiting is not None:
                run.previous, run.waiting = run.waiting, None
                run.released += 1
                self._cond.notify_all()

    def set_mode(self, draft_id: UUID, mode: StepMode | str) -> None:
        with self._cond:
            run = self._run(draft_id)
            run.mode, run.paused = StepMode(mode), False
            self._cond.notify_all()

    def pause(self, draft_id: UUID) -> None:
        """일시정지 (자동 진행): hold the run at the next stage boundary."""
        with self._cond:
            self._run(draft_id).paused = True

    def resume(self, draft_id: UUID) -> None:
        """계속 (자동 진행)."""
        with self._cond:
            self._run(draft_id).paused = False
            self._cond.notify_all()

    def search_trace(self, draft_id: UUID) -> SearchTrace | None:
        with self._cond:
            trace = self._run(draft_id).traces.get("search")
            return trace if isinstance(trace, SearchTrace) else None

    def duplicate_trace(self, draft_id: UUID) -> DuplicateCheckTrace | None:
        with self._cond:
            trace = self._run(draft_id).traces.get("duplicates")
            return trace if isinstance(trace, DuplicateCheckTrace) else None

    def wait_until_paused(self, draft_id: UUID, *, timeout: float) -> StepPoint:
        """Block until the run waits before a stage (tests, scripts)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            run = self._run(draft_id)
            if not self._cond.wait_for(lambda: run.waiting is not None,
                                       timeout=max(0.0, deadline - time.monotonic())):
                raise TimeoutError(f"draft {draft_id} did not pause within {timeout}s")
            assert run.waiting is not None
            return run.waiting
