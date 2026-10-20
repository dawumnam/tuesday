"""일괄 생성: run many 입력 조합 end to end and leave the drafts in 검토 대기.

The batch only stands in for the 담당자's *selection* (상황 후보 N, default the
first). It never reviews: 검토 라벨 are human-only (ADR 0002), so every draft it
makes waits in 검토 대기 for a person to label in the UI.

Combinations file: a JSON array of ``DraftInput`` objects, e.g.::

    [
      {"competency_ids": ["C4"], "audience": {"job": "재무·회계", "career_level": "3년차"},
       "difficulty": "medium", "memo": "법인카드 CSV"},
      ...
    ]
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from task_design.domain import Draft, DraftInput, DraftState
from task_design.service import TaskDesignService

_INPUTS = TypeAdapter(list[DraftInput])


def load_combinations(path: Path) -> list[DraftInput]:
    """Parse a combinations file; raises ``ValueError`` (pydantic) on a bad entry."""
    return _INPUTS.validate_python(json.loads(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class BatchItem:
    index: int
    input: DraftInput
    draft: Draft

    @property
    def ok(self) -> bool:
        return self.draft.state is DraftState.AWAITING_REVIEW and self.draft.error is None


def run_batch(
    service: TaskDesignService,
    inputs: Sequence[DraftInput],
    *,
    candidate: int = 1,
    author_ids: Sequence[int],
    on_done: Callable[[BatchItem], None] = lambda item: None,
) -> list[BatchItem]:
    """Start → select 상황 후보 ``candidate`` (1-based) → 검증, one combination at a time.

    Authors rotate through ``author_ids`` so each 담당자 can review the other's
    drafts (교차 검토). A combination whose run fails is reported and skipped; the
    rest still run.
    """
    items = []
    for i, draft_input in enumerate(inputs):
        draft = service.start(draft_input, author_ids[i % len(author_ids)])
        if draft.state is DraftState.AWAITING_SCENARIO_SELECTION and draft.error is None:
            index = min(candidate, len(draft.candidates)) - 1
            draft = service.select_scenario(draft.id, draft.candidates[index].id)
        item = BatchItem(index=i + 1, input=draft_input, draft=draft)
        on_done(item)
        items.append(item)
    return items
