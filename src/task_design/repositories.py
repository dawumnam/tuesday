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
    SearchQuery,
    Task,
)
from task_design.gateway import ModelGateway, Vector
from task_design.settings import Settings
from task_design.traces import SearchHit, SearchTrace, TermMatch

TaskSource = Literal["seed", "agent"]

# Reciprocal rank fusion constant (Cormack et al. 2009).
RRF_K = 60

# Minimum pg_trgm word_similarity(term, search_text) for a 검색어 term to match in the keyword arm
# (pg_trgm's own default similarity threshold). Below it the "match" is one or
# two incidental trigrams, e.g. a shared leading syllable.
KEYWORD_MIN_WORD_SIMILARITY = 0.3

_TASK_COLUMNS = (
    "id, scenario, instruction, attachments, rubric, competency_ids, audience, difficulty"
)
_attachments = TypeAdapter(list[Attachment])


def _vector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _float(x: object) -> float | None:
    return None if x is None else float(x)  # type: ignore[arg-type]


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
        self, query: SearchQuery | str, k: int = 5, *, pool: int = 20,
        draft_id: UUID | None = None,
    ) -> list[Task]:
        """유사 과제 검색: keyword (trigram) + vector rankings fused by RRF.

        ``query.text`` is embedded for the vector arm and given to the reranker.
        The keyword arm scores every ``query.terms`` entry on its own against
        상황 + 지시문 + 직무: a term counts for a task when its word similarity
        is >= ``KEYWORD_MIN_WORD_SIMILARITY``. Tasks are ranked by the number
        of matching terms, then the sum of their similarities; tasks matching no
        term are not ranked there at all (otherwise an arbitrary tie-break would
        rank them and their RRF score could push a strong semantic-only match
        out). A plain string is a single term.

        Each arm contributes its top ``max(pool, k)`` tasks. The top ``pool``
        fused tasks go to ``gateway.rerank`` and the first ``k`` of its order
        are returned.
        """
        tasks, _ = self._hybrid_search(
            SearchQuery.of(query), k, pool=pool, draft_id=draft_id, explain=False
        )
        return tasks

    def hybrid_search_traced(
        self, query: SearchQuery | str, k: int = 5, *, pool: int = 20,
        draft_id: UUID | None = None,
    ) -> tuple[list[Task], SearchTrace]:
        """``hybrid_search`` plus how it got there (every arm's ranks and scores, the
        matching terms, the RRF fusion and the rerank order), for the 데모 모드's
        step-by-step view."""
        tasks, trace = self._hybrid_search(
            SearchQuery.of(query), k, pool=pool, draft_id=draft_id, explain=True
        )
        assert trace is not None
        return tasks, trace

    def _hybrid_search(
        self, query: SearchQuery, k: int, *, pool: int, draft_id: UUID | None, explain: bool
    ) -> tuple[list[Task], SearchTrace | None]:
        arm = max(pool, k)
        terms = list(dict.fromkeys(t.strip() for t in query.terms if t.strip()))
        [q] = self._gateway.embed([query.text], draft_id=draft_id)
        with self._db.connection() as conn:
            # `term <% search_text` (word_similarity >= this threshold) can use the
            # trigram GiST index; transaction-local.
            conn.execute(
                "SELECT set_config('pg_trgm.word_similarity_threshold', %s, true)",
                (str(KEYWORD_MIN_WORD_SIMILARITY),),
            )
            rows = conn.execute(
                f"""
                WITH terms AS (
                    SELECT term, pos FROM unnest(%(terms)s::text[]) WITH ORDINALITY AS u(term, pos)
                ),
                matches AS (
                    SELECT t.id, terms.term, terms.pos,
                           word_similarity(terms.term, t.search_text) AS sim
                    FROM terms JOIN tasks t ON terms.term <%% t.search_text
                    WHERE t.status = 'active'
                ),
                keyword AS (
                    SELECT id, n, score, matches,
                           row_number() OVER (ORDER BY n DESC, score DESC, id) AS r
                    FROM (
                        SELECT id, count(*) AS n, sum(sim) AS score,
                               jsonb_agg(jsonb_build_object('term', term, 'similarity', sim)
                                         ORDER BY pos) AS matches
                        FROM matches WHERE sim >= %(min_sim)s GROUP BY id
                    ) g
                    ORDER BY n DESC, score DESC, id LIMIT %(arm)s
                ),
                semantic AS (
                    SELECT id, d, row_number() OVER (ORDER BY d, id) AS r
                    FROM (
                        SELECT id, embedding <=> %(v)s::vector AS d FROM tasks
                        WHERE status = 'active'
                        ORDER BY embedding <=> %(v)s::vector LIMIT %(arm)s
                    ) s
                ),
                fused AS (
                    SELECT id,
                           coalesce(1.0 / (%(rrf)s + k.r), 0)
                             + coalesce(1.0 / (%(rrf)s + s.r), 0) AS score,
                           k.r AS kr, k.score AS kw_score, k.matches AS kw_matches,
                           s.r AS sr, 1 - s.d AS cos
                    FROM keyword k FULL JOIN semantic s USING (id)
                )
                SELECT {", ".join("t." + c.strip() for c in _TASK_COLUMNS.split(","))},
                       f.score, f.kr, f.kw_score, f.kw_matches, f.sr, f.cos
                FROM fused f JOIN tasks t USING (id)
                ORDER BY f.score DESC, t.id
                LIMIT %(limit)s
                """,
                {
                    "terms": terms,
                    "min_sim": KEYWORD_MIN_WORD_SIMILARITY,
                    "v": _vector_literal(q),
                    "arm": arm,
                    "rrf": RRF_K,
                    "limit": None if explain else pool,  # the trace shows every ranked task
                },
            ).fetchall()
        n_task = len(_TASK_COLUMNS.split(","))
        fused = [_row_to_task(r[:n_task]) for r in rows[:pool]]
        final: list[int] = []
        if fused:
            order = self._gateway.rerank(
                query.text, [rerank_text(t) for t in fused], draft_id=draft_id
            )
            # Tolerate a sloppy reranker: drop bad/duplicate indices, keep the rest in fused order.
            for i in order:
                if 0 <= i < len(fused) and i not in final:
                    final.append(i)
            final += [i for i in range(len(fused)) if i not in final]
            final = final[:k]
        tasks = [fused[i] for i in final]
        if not explain:
            return tasks, None
        final_rank = {i: n for n, i in enumerate(final, start=1)}
        hits = []
        for n, r in enumerate(rows, start=1):
            score, kr, kw_score, kw_matches, sr, cos = r[n_task:]
            hits.append(SearchHit(
                task_id=r[0], scenario=r[1],
                keyword_rank=kr, keyword_score=_float(kw_score),
                keyword_matches=[TermMatch.model_validate(m) for m in kw_matches or []],
                semantic_rank=sr, cosine=_float(cos),
                fused_rank=n, fused_score=float(score), final_rank=final_rank.get(n - 1),
            ))
        return tasks, SearchTrace(
            query=query.text, terms=terms,
            keyword_min_word_similarity=KEYWORD_MIN_WORD_SIMILARITY, rrf_k=RRF_K,
            rerank_pool=pool, k=k, hits=hits,
        )

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

    def nearest_to_vector(self, vector: Vector) -> SimilarTask | None:
        """The most similar active task (any similarity); for the 중복 검사 trace."""
        with self._db.connection() as conn:
            row = conn.execute(
                f"""
                SELECT {_TASK_COLUMNS}, 1 - (embedding <=> %(v)s::vector) AS similarity
                FROM tasks WHERE status = 'active'
                ORDER BY embedding <=> %(v)s::vector, id LIMIT 1
                """,
                {"v": _vector_literal(vector)},
            ).fetchone()
        return SimilarTask(task=_row_to_task(row[:-1]), similarity=row[-1]) if row else None

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
