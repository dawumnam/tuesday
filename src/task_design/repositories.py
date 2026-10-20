"""과제 저장소 (TaskRepository) and 역량 체계 저장소 (CompetencyRepository)."""

from __future__ import annotations

import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, TypeAdapter

from task_design.db import Database
from task_design.domain import (
    Attachment,
    Audience,
    Competency,
    Level,
    LevelIndicator,
    Rubric,
    Task,
)
from task_design.gateway import ModelGateway, Vector
from task_design.settings import Settings

TaskSource = Literal["seed", "agent"]

# Reciprocal rank fusion constant (Cormack et al. 2009).
RRF_K = 60

# Minimum pg_trgm word_similarity(query, search_text) for the keyword arm
# (pg_trgm's own default similarity threshold). Below it the "match" is one or
# two incidental trigrams, e.g. a shared leading syllable.
KEYWORD_MIN_WORD_SIMILARITY = 0.3

_TASK_COLUMNS = (
    "id, scenario, instruction, attachments, rubric, competency_ids, audience, difficulty"
)
_attachments = TypeAdapter(list[Attachment])


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _row_to_task(row: tuple) -> Task:
    id_, scenario, instruction, attachments, rubric, competency_ids, audience, difficulty = row
    return Task(
        id=id_,
        scenario=scenario,
        instruction=instruction,
        attachments=_attachments.validate_python(attachments),
        rubric=Rubric.model_validate(rubric),
        competency_ids=list(competency_ids),
        audience=Audience.model_validate(audience),
        difficulty=Level(difficulty),
    )


class SimilarTask(BaseModel):
    task: Task
    similarity: float  # cosine similarity, 1.0 = identical direction


def rerank_text(task: Task) -> str:
    """The document text the reranker sees for a task."""
    return f"{task.scenario}\n{task.instruction}"


class TaskRepository:
    """과제 DB: confirmed and seeded tasks, with hybrid search and duplicate checks.

    A task's embedding is ``gateway.embed([task.scenario])``: the 상황 is what
    상황 후보 are compared against, so duplicates are judged scenario to scenario.
    Keyword (trigram) search runs over 상황 + 지시문.
    """

    def __init__(self, db: Database, gateway: ModelGateway, settings: Settings) -> None:
        self._db = db
        self._gateway = gateway
        self._settings = settings

    # --- writes ------------------------------------------------------------------

    def add(
        self, task: Task, *, source: TaskSource = "agent", draft_id: UUID | None = None
    ) -> UUID:
        """Insert (or, if ``task.id`` already exists, replace) a task. Returns its id."""
        task_id = task.id or uuid4()
        [embedding] = self._gateway.embed([task.scenario], draft_id=draft_id)
        with self._db.connection() as conn:
            conn.execute(
                f"""
                INSERT INTO tasks ({_TASK_COLUMNS}, embedding, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                ON CONFLICT (id) DO UPDATE SET
                    scenario = EXCLUDED.scenario,
                    instruction = EXCLUDED.instruction,
                    attachments = EXCLUDED.attachments,
                    rubric = EXCLUDED.rubric,
                    competency_ids = EXCLUDED.competency_ids,
                    audience = EXCLUDED.audience,
                    difficulty = EXCLUDED.difficulty,
                    embedding = EXCLUDED.embedding,
                    source = EXCLUDED.source,
                    status = 'active'
                """,
                (
                    task_id,
                    task.scenario,
                    task.instruction,
                    json.dumps([a.model_dump(mode="json") for a in task.attachments]),
                    json.dumps(task.rubric.model_dump(mode="json")),
                    task.competency_ids,
                    json.dumps(task.audience.model_dump(mode="json")),
                    task.difficulty.value,
                    _vector_literal(embedding),
                    source,
                ),
            )
        return task_id

    # --- reads -------------------------------------------------------------------

    def hybrid_search(
        self, query: str, k: int = 5, *, pool: int = 20, draft_id: UUID | None = None
    ) -> list[Task]:
        """유사 과제 검색: trigram + vector rankings fused by RRF.

        Each arm contributes its top ``max(pool, k)`` tasks. The keyword arm
        only ranks real keyword matches (word similarity >=
        ``KEYWORD_MIN_WORD_SIMILARITY``): otherwise non-matching tasks would be
        ranked there by an arbitrary tie-break and their RRF score could push a
        strong semantic-only match out of the results. The top ``pool``
        fused tasks go to ``gateway.rerank`` and the first ``k`` of its order
        are returned.
        """
        arm = max(pool, k)
        [q] = self._gateway.embed([query], draft_id=draft_id)
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                WITH keyword AS (
                    SELECT id, row_number() OVER (ORDER BY %(q)s <<-> search_text, id) AS r
                    FROM (
                        SELECT id, search_text FROM tasks
                        WHERE status = 'active' AND %(q)s <<-> search_text <= %(max_kw_dist)s
                        ORDER BY %(q)s <<-> search_text LIMIT %(arm)s
                    ) s
                ),
                semantic AS (
                    SELECT id, row_number() OVER (ORDER BY d, id) AS r
                    FROM (
                        SELECT id, embedding <=> %(v)s::vector AS d FROM tasks
                        WHERE status = 'active'
                        ORDER BY embedding <=> %(v)s::vector LIMIT %(arm)s
                    ) s
                ),
                fused AS (
                    SELECT id, sum(1.0 / (%(rrf)s + r)) AS score
                    FROM (SELECT * FROM keyword UNION ALL SELECT * FROM semantic) u
                    GROUP BY id
                )
                SELECT {", ".join("t." + c.strip() for c in _TASK_COLUMNS.split(","))}
                FROM fused f JOIN tasks t USING (id)
                ORDER BY f.score DESC, t.id
                LIMIT %(pool)s
                """,
                {
                    "q": query,
                    "v": _vector_literal(q),
                    "arm": arm,
                    "max_kw_dist": 1 - KEYWORD_MIN_WORD_SIMILARITY,
                    "rrf": RRF_K,
                    "pool": pool,
                },
            ).fetchall()
        fused = [_row_to_task(r) for r in rows]
        if not fused:
            return []
        order = self._gateway.rerank(
            query, [rerank_text(t) for t in fused], draft_id=draft_id
        )
        # Tolerate a sloppy reranker: drop bad/duplicate indices, keep the rest in fused order.
        seen: list[int] = []
        for i in order:
            if 0 <= i < len(fused) and i not in seen:
                seen.append(i)
        seen += [i for i in range(len(fused)) if i not in seen]
        return [fused[i] for i in seen[:k]]

    def find_duplicates(
        self, text: str, threshold: float | None = None, *, draft_id: UUID | None = None
    ) -> list[SimilarTask]:
        """Active tasks whose embedding cosine similarity to ``text`` is >= threshold.

        ``text`` is normally a 상황 후보's scenario. ``threshold`` defaults to
        ``settings.duplicate_threshold``. Most similar first. Exact scan (no ANN).
        """
        [v] = self._gateway.embed([text], draft_id=draft_id)
        return self.find_duplicates_of_vector(v, threshold)

    def find_duplicates_of_vector(
        self, vector: Vector, threshold: float | None = None
    ) -> list[SimilarTask]:
        """``find_duplicates`` for an already-embedded text (no gateway call)."""
        threshold = self._settings.duplicate_threshold if threshold is None else threshold
        v = vector
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT {_TASK_COLUMNS}, 1 - (embedding <=> %(v)s::vector) AS similarity
                FROM tasks
                WHERE status = 'active' AND 1 - (embedding <=> %(v)s::vector) >= %(t)s
                ORDER BY similarity DESC, id
                """,
                {"v": _vector_literal(v), "t": threshold},
            ).fetchall()
        return [SimilarTask(task=_row_to_task(r[:-1]), similarity=r[-1]) for r in rows]

    def get(self, task_id: UUID) -> Task | None:
        with self._db.connection() as conn:
            row = conn.execute(
                f"SELECT {_TASK_COLUMNS} FROM tasks WHERE id = %s", (task_id,)
            ).fetchone()
        return _row_to_task(row) if row else None


class CompetencyRepository:
    """역량 체계 저장소: read-only lookups. Data arrives via ``task_design.loader``."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, competency_id: str) -> Competency | None:
        found = self._load("WHERE c.id = %s", (competency_id,))
        return found[0] if found else None

    def list(self) -> list[Competency]:
        return self._load("", ())

    def level_indicators(self, competency_id: str, level: Level) -> list[LevelIndicator]:
        """One 수준 지표 per sub-element of the competency at ``level``, in sub-element order."""
        with self._db.connection() as conn:
            rows = conn.execute(
                """
                SELECT s.competency_id, s.id, s.name, i.level, i.description
                FROM competency_sub_elements s
                JOIN level_indicators i ON i.sub_element_id = s.id
                WHERE s.competency_id = %s AND i.level = %s
                ORDER BY s.position, s.id
                """,
                (competency_id, Level(level).value),
            ).fetchall()
        return [
            LevelIndicator(
                competency_id=c, sub_element_id=sid, sub_element_name=n, level=Level(lv), description=d
            )
            for c, sid, n, lv, d in rows
        ]

    def _load(self, where: str, params: tuple) -> list[Competency]:
        with self._db.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT c.id, c.name, c.description, s.id, s.name, s.description, i.level, i.description
                FROM competencies c
                LEFT JOIN competency_sub_elements s ON s.competency_id = c.id
                LEFT JOIN level_indicators i ON i.sub_element_id = s.id
                {where}
                ORDER BY c.id, s.position, s.id
                """,
                params,
            ).fetchall()
        competencies: dict[str, dict] = {}
        subs: dict[str, dict] = {}
        for cid, cname, cdesc, sid, sname, sdesc, level, idesc in rows:
            comp = competencies.setdefault(
                cid, {"id": cid, "name": cname, "description": cdesc, "sub_elements": []}
            )
            if sid is None:
                continue
            if sid not in subs:
                subs[sid] = {"id": sid, "name": sname, "description": sdesc, "indicators": {}}
                comp["sub_elements"].append(subs[sid])
            subs[sid]["indicators"][level] = idesc
        return [Competency.model_validate(c) for c in competencies.values()]
