"""유사 과제 검색 평가: does the search find the tasks a person says it should?

A query set is a JSON array; every entry is an 입력 조합 (the same ``DraftInput`` a
담당자 types in) plus the ids of the 과제 DB tasks that are the right 유사 과제 for it::

    [
      {"input": {"competency_ids": ["C4"],
                 "audience": {"job": "재무·회계", "career_level": "3년차"},
                 "difficulty": "medium", "memo": null},
       "expected": ["1c47453d-28cb-5071-8bae-69513a00ead9"]}
    ]

Each query runs through the real search path (the 검색어 the workflow builds, then
``TaskRepository.hybrid_search_traced``). A query is a hit at a step when at least
one expected task is in that step's top ``k``. The steps are the keyword arm, the
vector arm, their RRF fusion, and the final list after the LLM 리랭크, so the
numbers show which step loses a task.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, Field, TypeAdapter

from task_design.domain import DraftInput
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.traces import SearchHit
from task_design.workflow.prompts import similar_task_query

# Step → Korean name, in pipeline order.
STEPS = {"keyword": "키워드", "semantic": "벡터", "fused": "RRF", "final": "리랭크 후"}


class EvalQuery(BaseModel):
    input: DraftInput
    expected: list[UUID] = Field(min_length=1)
    note: str | None = None


class QueryResult(BaseModel):
    query: EvalQuery
    text: str  # the 검색어 text that was searched
    # Step → best (lowest) 1-based rank of an expected task; None = not ranked there.
    ranks: dict[str, int | None]
    hits: dict[str, bool]  # step → an expected task is in its top k


class EvalResult(BaseModel):
    k: int
    results: list[QueryResult]

    def hit_count(self, step: str) -> int:
        return sum(r.hits[step] for r in self.results)

    def hit_rate(self, step: str) -> float:
        return self.hit_count(step) / len(self.results) if self.results else 0.0


_QUERIES = TypeAdapter(list[EvalQuery])


def load_queries(path: Path) -> list[EvalQuery]:
    """Parse a query set file; raises ``ValueError`` (pydantic) on a bad entry."""
    return _QUERIES.validate_python(json.loads(Path(path).read_text(encoding="utf-8")))


def _rank(hit: SearchHit, step: str) -> int | None:
    return {"keyword": hit.keyword_rank, "semantic": hit.semantic_rank,
            "fused": hit.fused_rank, "final": hit.final_rank}[step]


def evaluate(
    tasks: TaskRepository,
    competencies: CompetencyRepository,
    queries: Sequence[EvalQuery],
    *,
    k: int = 5,
    pool: int = 20,
) -> EvalResult:
    names = {c.id: c for c in competencies.list()}
    results = []
    for q in queries:
        comps = [names[cid] for cid in q.input.competency_ids if cid in names]
        search = similar_task_query(q.input, comps)
        _, trace = tasks.hybrid_search_traced(search, k=k, pool=pool)
        expected = set(q.expected)
        ranks: dict[str, int | None] = {}
        for step in STEPS:
            found = [r for h in trace.hits
                     if h.task_id in expected and (r := _rank(h, step)) is not None]
            ranks[step] = min(found, default=None)
        results.append(QueryResult(
            query=q, text=search.text, ranks=ranks,
            hits={step: rank is not None and rank <= k for step, rank in ranks.items()},
        ))
    return EvalResult(k=k, results=results)
