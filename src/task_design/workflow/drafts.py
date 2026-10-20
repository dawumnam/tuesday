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
    DraftRound,
    DraftState,
    FailureReport,
    Level,
    PersonaStatus,
    ScenarioCandidate,
    Stage,
    Task,
)

_COLUMNS = (
    "id, author_id, input, state, stage, similar_tasks, candidates, "
    "selected_candidate_id, error, created_at, updated_at, "
    "task, rounds, persona_status, failure_report"
)
_TASKS = TypeAdapter(list[Task])
_CANDIDATES = TypeAdapter(list[ScenarioCandidate])
_ROUNDS = TypeAdapter(list[DraftRound])
_PERSONA_STATUS = TypeAdapter(dict[Level, PersonaStatus])


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
         updated, task, rounds, persona_status, failure_report) = row
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
            task=Task.model_validate(task) if task is not None else None,
            rounds=_ROUNDS.validate_python(rounds),
            persona_status=_PERSONA_STATUS.validate_python(persona_status),
            failure_report=(
                FailureReport.model_validate(failure_report) if failure_report is not None else None
            ),
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

    def claim_retry(self, draft_id: UUID) -> None:
        """Clear the error of a failed run so it can be retried; atomic, so one retry wins.

        Raises ``InvalidDraftState`` if the draft's last run did not fail.
        """
        with self._db.connection() as conn:
            updated = conn.execute(
                "UPDATE drafts SET error = NULL, updated_at = now() "
                "WHERE id = %s AND error IS NOT NULL",
                (draft_id,),
            ).rowcount
        if not updated:
            self.get(draft_id)  # raises DraftNotFound
            raise InvalidDraftState("실패한 실행이 없어 다시 시도할 수 없습니다.")

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

    def set_task(self, draft_id: UUID, task: Task) -> None:
        self._update(draft_id, "task = %s", task.model_dump_json())

    def save_round(self, draft_id: UUID, round: DraftRound) -> None:
        """Insert or replace (by ``number``) one 초안 회차; rounds stay ordered by number."""
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT rounds FROM drafts WHERE id = %s FOR UPDATE", (draft_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFound(f"no draft {draft_id}")
            rounds = [r for r in _ROUNDS.validate_python(row[0]) if r.number != round.number]
            rounds = sorted([*rounds, round], key=lambda r: r.number)
            conn.execute(
                "UPDATE drafts SET rounds = %s, updated_at = now() WHERE id = %s",
                (_dump(_ROUNDS, rounds), draft_id),
            )

    def reset_persona_status(self, draft_id: UUID) -> None:
        """All three personas → 대기 (start of a round's 모의 응시)."""
        self._update(
            draft_id, "persona_status = %s",
            _dump(_PERSONA_STATUS, dict.fromkeys(Level, PersonaStatus.PENDING)),
        )

    def set_persona_status(self, draft_id: UUID, level: Level, status: PersonaStatus) -> None:
        """Update one persona's status; atomic, so parallel personas don't clobber each other."""
        self._update(
            draft_id, "persona_status = persona_status || jsonb_build_object(%s::text, %s::text)",
            (level.value, status.value),
        )

    def set_failure_report(self, draft_id: UUID, report: FailureReport | None) -> None:
        self._update(
            draft_id, "failure_report = %s", report.model_dump_json() if report else None
        )

    def _update(self, draft_id: UUID, assignment: str, value: object) -> None:
        """``value``: one parameter, or a tuple for an assignment with several."""
        params = value if isinstance(value, tuple) else (value,)
        with self._db.connection() as conn:
            conn.execute(
                f"UPDATE drafts SET {assignment}, updated_at = now() WHERE id = %s",
                (*params, draft_id),
            )
