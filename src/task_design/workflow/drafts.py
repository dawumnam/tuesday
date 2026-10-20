"""DraftStore: the ``drafts`` table (migration 0004).

The workflow writes the draft's observable state here as it goes; ``get_draft``
reads only this table, never the LangGraph checkpoints. Every write commits on
its own, so a ``get_draft`` from another thread sees progress live.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from uuid import UUID

from pydantic import TypeAdapter

from task_design.db import Database
from task_design.domain import (
    Draft,
    DraftErrorKind,
    DraftInput,
    DraftRound,
    DraftState,
    DraftSummary,
    FailureReport,
    Level,
    PersonaStatus,
    Review,
    ReviewDecision,
    ScenarioCandidate,
    Stage,
    Task,
)

_COLUMNS = (
    "id, author_id, input, state, stage, similar_tasks, candidates, "
    "selected_candidate_id, error, created_at, updated_at, "
    "task, rounds, persona_status, failure_report, error_kind"
)
_TASKS = TypeAdapter(list[Task])
_CANDIDATES = TypeAdapter(list[ScenarioCandidate])
_ROUNDS = TypeAdapter(list[DraftRound])
_PERSONA_STATUS = TypeAdapter(dict[Level, PersonaStatus])


# Exception messages are Korean: the UI shows them as-is (ticket 10). Tests
# assert on the exception type, never on the text.


class DraftNotFound(LookupError):
    """No draft with that id."""


class InvalidDraftState(RuntimeError):
    """The operation is not allowed in the draft's current state."""


class UnknownCandidate(LookupError):
    """The 상황 후보 is not among the draft's current candidates."""


def _load_review(conn, draft_id: UUID) -> Review | None:
    row = conn.execute(
        "SELECT reviewer_id, decision, edited_task, reviewed_at FROM reviews WHERE draft_id = %s",
        (draft_id,),
    ).fetchone()
    if row is None:
        return None
    labels = conn.execute(
        "SELECT component, label FROM review_labels WHERE draft_id = %s", (draft_id,)
    ).fetchall()
    reviewer_id, decision, edited_task, reviewed_at = row
    return Review(
        reviewer_id=reviewer_id,
        labels=dict(labels),
        decision=ReviewDecision(decision),
        edited_task=edited_task,
        reviewed_at=reviewed_at,
    )


def _input_title(input: DraftInput) -> str:
    return (f"{', '.join(input.competency_ids)} · {input.audience.job} "
            f"{input.audience.career_level} · 난이도 {input.difficulty.ko}")


def _edited_json(edited: Task | dict | None) -> str | None:
    if edited is None:
        return None
    return edited.model_dump_json() if isinstance(edited, Task) else json.dumps(edited)


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
            review = _load_review(conn, draft_id) if row is not None else None
        if row is None:
            raise DraftNotFound("초안을 찾을 수 없습니다.")
        (id_, author, input_, state, stage, similar, candidates, selected, error, created,
         updated, task, rounds, persona_status, failure_report, error_kind) = row
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
            error_kind=DraftErrorKind(error_kind) if error_kind else None,
            task=Task.model_validate(task) if task is not None else None,
            rounds=_ROUNDS.validate_python(rounds),
            persona_status=_PERSONA_STATUS.validate_python(persona_status),
            failure_report=(
                FailureReport.model_validate(failure_report) if failure_report is not None else None
            ),
            review=review,
            created_at=created,
            updated_at=updated,
        )

    def list(
        self,
        *,
        author_id: int | None = None,
        states: Sequence[DraftState] | None = None,
        limit: int = 200,
    ) -> list[DraftSummary]:
        """Drafts newest first, optionally only one author's and/or only some states."""
        where, params = [], []
        if author_id is not None:
            where.append("author_id = %s")
            params.append(author_id)
        if states is not None:
            where.append("state = ANY(%s)")
            params.append([s.value for s in states])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        with self._db.connection() as conn:
            rows = conn.execute(
                "SELECT id, author_id, state, stage, input, candidates, selected_candidate_id, "
                f"error IS NOT NULL, created_at, updated_at FROM drafts {clause} "
                "ORDER BY created_at DESC, id LIMIT %s",
                (*params, limit),
            ).fetchall()
        out = []
        for (id_, author, state, stage, input_, candidates, selected, has_error, created,
             updated) in rows:
            input_model = DraftInput.model_validate(input_)
            title = next(
                (c["title"] for c in candidates if selected and c.get("id") == str(selected)),
                None,
            ) or _input_title(input_model)
            out.append(DraftSummary(
                id=id_, author_id=author, state=DraftState(state),
                stage=Stage(stage) if stage else None, title=title, input=input_model,
                has_error=has_error, created_at=created, updated_at=updated,
            ))
        return out

    def transition(self, draft_id: UUID, expected: DraftState, new: DraftState) -> None:
        """Atomically move ``expected`` → ``new``; raise if the draft is elsewhere."""
        with self._db.connection() as conn:
            cur = conn.execute(
                "UPDATE drafts SET state = %s, error = NULL, error_kind = NULL, "
                "updated_at = now() WHERE id = %s AND state = %s",
                (new.value, draft_id, expected.value),
            )
            updated = cur.rowcount
        if not updated:
            draft = self.get(draft_id)  # raises DraftNotFound
            raise InvalidDraftState(
                f"초안이 '{draft.state.ko}' 상태라 할 수 없습니다. '{expected.ko}' 상태에서만 "
                "가능합니다."
            )

    def select_candidate(self, draft_id: UUID, candidate_id: UUID) -> None:
        """상황 선택 대기 → 검증 중, recording the chosen 상황 후보, in one statement.

        The candidate must be among the current candidates (checked atomically,
        so a concurrent regeneration can't slip a stale id through). Raises
        ``InvalidDraftState`` or ``UnknownCandidate``.
        """
        with self._db.connection() as conn:
            cur = conn.execute(
                "UPDATE drafts SET state = %s, selected_candidate_id = %s, error = NULL, error_kind = NULL, "
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
                f"초안이 '{draft.state.ko}' 상태라 상황을 고를 수 없습니다. "
                f"'{DraftState.AWAITING_SCENARIO_SELECTION.ko}' 상태에서만 가능합니다."
            )
        raise UnknownCandidate("현재 제시된 상황 후보가 아닙니다. 후보 목록을 새로 불러와 다시 고르세요.")

    def save_review(
        self, draft_id: UUID, review: Review, *, confirm: Callable[[], UUID] | None = None
    ) -> None:
        """검토 대기 → 확정 | 반려, storing the 최종 검토 and its 검토 라벨, atomically.

        The draft row is locked for the whole operation, so of two concurrent
        reviews exactly one wins and the other raises ``InvalidDraftState``.
        ``confirm`` (확정 only) runs under that lock, before anything is
        written, and returns the 확정 과제 id; if it raises, nothing is stored.
        """
        new_state = (
            DraftState.CONFIRMED if review.decision is ReviewDecision.CONFIRM
            else DraftState.REJECTED
        )
        with self._db.connection() as conn:
            row = conn.execute(
                "SELECT state FROM drafts WHERE id = %s FOR UPDATE", (draft_id,)
            ).fetchone()
            if row is None:
                raise DraftNotFound("초안을 찾을 수 없습니다.")
            state = DraftState(row[0])
            if state is not DraftState.AWAITING_REVIEW:
                raise InvalidDraftState(
                    f"초안이 '{state.ko}' 상태라 검토할 수 없습니다. "
                    f"'{DraftState.AWAITING_REVIEW.ko}' 상태에서만 가능합니다."
                )
            task_id = confirm() if confirm is not None else None
            conn.execute(
                "INSERT INTO reviews (draft_id, reviewer_id, decision, edited_task, task_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (draft_id, review.reviewer_id, review.decision.value,
                 _edited_json(review.edited_task), task_id),
            )
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO review_labels (draft_id, component, label) VALUES (%s, %s, %s)",
                    [(draft_id, c.value, label.value) for c, label in review.labels.items()],
                )
            conn.execute(
                "UPDATE drafts SET state = %s, stage = NULL, updated_at = now() WHERE id = %s",
                (new_state.value, draft_id),
            )

    def claim_retry(self, draft_id: UUID) -> None:
        """Clear the error of a failed run so it can be retried; atomic, so one retry wins.

        Raises ``InvalidDraftState`` if the draft's last run did not fail.
        """
        with self._db.connection() as conn:
            updated = conn.execute(
                "UPDATE drafts SET error = NULL, error_kind = NULL, updated_at = now() "
                "WHERE id = %s AND error IS NOT NULL",
                (draft_id,),
            ).rowcount
        if not updated:
            self.get(draft_id)  # raises DraftNotFound
            raise InvalidDraftState("실패한 실행이 없어 다시 시도할 수 없습니다.")

    def mark_interrupted(self, message: str) -> list[UUID]:
        """Give every draft left running (생성 중 / 검증 중, no error) the error ``message``
        of kind ``DraftErrorKind.INTERRUPTED``."""
        with self._db.connection() as conn:
            rows = conn.execute(
                "UPDATE drafts SET error = %s, error_kind = %s, stage = NULL, updated_at = now() "
                "WHERE state = ANY(%s) AND error IS NULL RETURNING id",
                (message, DraftErrorKind.INTERRUPTED.value,
                 [DraftState.GENERATING.value, DraftState.VERIFYING.value]),
            ).fetchall()
        return [r[0] for r in rows]

    def set_state(self, draft_id: UUID, state: DraftState) -> None:
        self._update(draft_id, "state = %s, stage = NULL", state.value)

    def set_stage(self, draft_id: UUID, stage: Stage | None) -> None:
        self._update(draft_id, "stage = %s", stage.value if stage else None)

    def set_error(self, draft_id: UUID, error: str, kind: DraftErrorKind) -> None:
        self._update(draft_id, "error = %s, error_kind = %s", (error, kind.value))

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
                raise DraftNotFound("초안을 찾을 수 없습니다.")
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
