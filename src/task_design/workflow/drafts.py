"""DraftStore: the ``drafts`` table (migration 0004).

The workflow writes the draft's observable state here as it goes; ``get_draft``
reads only this table, never the LangGraph checkpoints. Every write commits on
its own, so a ``get_draft`` from another thread sees progress live.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import TypeAdapter

from task_design.db import Database
from task_design.domain import (
    Draft,
    DraftInput,
    DraftState,
    ScenarioCandidate,
    Stage,
    Task,
)

_COLUMNS = (
    "id, author_id, input, state, stage, similar_tasks, candidates, "
    "selected_candidate_id, error, created_at, updated_at"
)
_TASKS = TypeAdapter(list[Task])
_CANDIDATES = TypeAdapter(list[ScenarioCandidate])


class DraftNotFound(LookupError):
    pass


class InvalidDraftState(RuntimeError):
    """The operation is not allowed in the draft's current state."""


class UnknownCandidate(LookupError):
    """The 상황 후보 is not among the draft's current candidates."""


def _dump(adapter: TypeAdapter, value: object) -> str:
    return adapter.dump_json(value).decode()


class DraftStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create(self, draft_id: UUID, author_id: int, input: DraftInput) -> None:
        with self._db.connection() as conn:
            conn.execute(
                "INSERT INTO drafts (id, author_id, input, state) VALUES (%s, %s, %s, %s)",
                (draft_id, author_id, input.model_dump_json(), DraftState.GENERATING.value),
            )

    def get(self, draft_id: UUID) -> Draft:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM drafts WHERE id = %s", (draft_id,)
            ).fetchone()
        if row is None:
            raise DraftNotFound(f"no draft {draft_id}")
        (id_, author, input_, state, stage, similar, candidates, selected, error, created,
         updated) = row
        return Draft(
            id=id_,
            author_id=author,
            input=DraftInput.model_validate(input_),
            state=DraftState(state),
            stage=Stage(stage) if stage else None,
            similar_tasks=_TASKS.validate_python(similar),
            candidates=_CANDIDATES.validate_python(candidates),
            selected_candidate_id=selected,
            error=error,
            created_at=created,
            updated_at=updated,
        )

    def transition(self, draft_id: UUID, expected: DraftState, new: DraftState) -> None:
        """Atomically move ``expected`` → ``new``; raise if the draft is elsewhere."""
        with self._db.connection() as conn:
            cur = conn.execute(
                "UPDATE drafts SET state = %s, error = NULL, updated_at = now() "
                "WHERE id = %s AND state = %s",
                (new.value, draft_id, expected.value),
            )
            updated = cur.rowcount
        if not updated:
            draft = self.get(draft_id)  # raises DraftNotFound
            raise InvalidDraftState(
                f"draft {draft_id} is {draft.state.ko}, expected {expected.ko}"
            )

    def select_candidate(self, draft_id: UUID, candidate_id: UUID) -> None:
        """상황 선택 대기 → 검증 중, recording the chosen 상황 후보, in one statement.

        The candidate must be among the current candidates (checked atomically,
        so a concurrent regeneration can't slip a stale id through). Raises
        ``InvalidDraftState`` or ``UnknownCandidate``.
        """
        with self._db.connection() as conn:
            cur = conn.execute(
                "UPDATE drafts SET state = %s, selected_candidate_id = %s, error = NULL, "
                "updated_at = now() "
                "WHERE id = %s AND state = %s "
                "AND candidates @> jsonb_build_array(jsonb_build_object('id', %s::text))",
                (DraftState.VERIFYING.value, candidate_id, draft_id,
                 DraftState.AWAITING_SCENARIO_SELECTION.value, str(candidate_id)),
            )
            updated = cur.rowcount
        if updated:
            return
        draft = self.get(draft_id)
        if draft.state is not DraftState.AWAITING_SCENARIO_SELECTION:
            raise InvalidDraftState(
                f"draft {draft_id} is {draft.state.ko}, "
                f"expected {DraftState.AWAITING_SCENARIO_SELECTION.ko}"
            )
        raise UnknownCandidate(f"{candidate_id} is not a current 상황 후보 of draft {draft_id}")

    def set_state(self, draft_id: UUID, state: DraftState) -> None:
        self._update(draft_id, "state = %s, stage = NULL", state.value)

    def set_stage(self, draft_id: UUID, stage: Stage | None) -> None:
        self._update(draft_id, "stage = %s", stage.value if stage else None)

    def set_error(self, draft_id: UUID, error: str) -> None:
        self._update(draft_id, "error = %s", error)

    def set_similar_tasks(self, draft_id: UUID, tasks: list[Task]) -> None:
        self._update(draft_id, "similar_tasks = %s", _dump(_TASKS, tasks))

    def set_candidates(self, draft_id: UUID, candidates: list[ScenarioCandidate]) -> None:
        self._update(draft_id, "candidates = %s", _dump(_CANDIDATES, candidates))

    def _update(self, draft_id: UUID, assignment: str, value: object) -> None:
        with self._db.connection() as conn:
            conn.execute(
                f"UPDATE drafts SET {assignment}, updated_at = now() WHERE id = %s",
                (value, draft_id),
            )
