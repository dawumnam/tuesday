"""Explain-output of 유사 과제 검색 (RAG) and 상황 후보 중복 검사.

Built only when a run observer is installed (데모 모드), see
``task_design.workflow.observer``.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class TermMatch(BaseModel):
    """One 검색어 term that matched a task in the keyword arm."""

    term: str
    similarity: float  # pg_trgm word_similarity(term, 상황+지시문+직무)


class SearchHit(BaseModel):
    """One task that either search arm ranked, with its place in every step."""

    task_id: UUID
    scenario: str
    keyword_rank: int | None = None  # 1-based rank in the trigram arm (None: no match)
    keyword_matches: list[TermMatch] = []  # terms at or above the threshold, in query order
    keyword_score: float | None = None  # Σ similarity over keyword_matches
    semantic_rank: int | None = None  # 1-based rank in the vector arm
    cosine: float | None = None  # cosine similarity of the embeddings
    fused_rank: int  # 1-based rank after RRF
    fused_score: float  # Σ 1/(rrf_k + rank) over the arms that ranked it
    final_rank: int | None = None  # 1-based rank after the LLM 리랭크 (None: cut)


class SearchTrace(BaseModel):
    kind: Literal["search"] = "search"
    query: str  # embedded for the vector arm and given to the reranker
    terms: list[str]  # the keyword arm's terms, each scored separately
    keyword_min_word_similarity: float  # per term
    rrf_k: int
    rerank_pool: int  # top fused tasks sent to the reranker
    k: int  # tasks kept after reranking
    hits: list[SearchHit]  # in fused order

    def final(self) -> list[SearchHit]:
        """The 유사 과제 passed to the 상황 후보 generator, in reranked order."""
        return sorted((h for h in self.hits if h.final_rank is not None),
                      key=lambda h: h.final_rank or 0)


ProposalOutcome = Literal["accepted", "db_duplicate", "peer_duplicate", "already_shown"]


class ProposalCheck(BaseModel):
    """One proposed 상황 후보 and the verdict of the duplicate check."""

    attempt: int  # 1-based generation call
    title: str
    scenario: str
    outcome: ProposalOutcome
    max_db_similarity: float | None = None  # cosine to the most similar active task
    nearest_task_id: UUID | None = None
    nearest_task_scenario: str | None = None
    max_peer_similarity: float | None = None  # cosine to an already-accepted proposal


class DuplicateCheckTrace(BaseModel):
    kind: Literal["duplicates"] = "duplicates"
    threshold: float
    proposals: list[ProposalCheck]

    def accepted(self) -> list[ProposalCheck]:
        return [p for p in self.proposals if p.outcome == "accepted"]
