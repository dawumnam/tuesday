"""Model gateway seam: every model call goes through ``ModelGateway``.

Ticket 02 provides the real implementation; tests use
``task_design.testing.fake_gateway.FakeGateway``.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, Protocol, overload, runtime_checkable
from uuid import UUID

from pydantic import BaseModel

Vector = list[float]


class Role(StrEnum):
    GENERATION = "generation"  # 생성 (scenario, instruction, attachments, rubric, fixes)
    REVIEW = "review"  # 검토 에이전트
    EXAMINEE = "examinee"  # 응시자 페르소나
    AI_TOOL = "ai_tool"  # AI 도구 LLM
    GRADING = "grading"  # 채점 에이전트
    RERANK = "rerank"  # 리랭크
    EMBEDDING = "embedding"  # 임베딩


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class Usage(BaseModel):
    """Token usage of one model call. Accumulated per draft (spec: 비용)."""

    role: Role
    draft_id: UUID | None
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float = 0.0


class UsageSink(Protocol):
    """Where gateways report usage. Ticket 02 persists it to ``model_costs``."""

    def record(self, usage: Usage) -> None: ...


@runtime_checkable
class ModelGateway(Protocol):
    @overload
    def complete[T: BaseModel](
        self,
        role: Role,
        messages: Sequence[Message],
        schema: type[T],
        draft_id: UUID | None = None,
    ) -> T: ...

    @overload
    def complete(
        self,
        role: Role,
        messages: Sequence[Message],
        schema: None = None,
        draft_id: UUID | None = None,
    ) -> str: ...

    def complete(
        self,
        role: Role,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        draft_id: UUID | None = None,
    ) -> BaseModel | str:
        """Run one completion for ``role``.

        With ``schema``: the output is validated into an instance of ``schema``
        (the real gateway retries on invalid output). Without: plain text
        (used for free-form turns such as examinee ↔ AI tool conversation).
        """
        ...

    def embed(self, texts: Sequence[str], draft_id: UUID | None = None) -> list[Vector]:
        """One vector (length ``settings.embedding_dim``) per input text."""
        ...

    def rerank(
        self, query: str, docs: Sequence[str], draft_id: UUID | None = None
    ) -> list[int]:
        """Indices into ``docs``, best first (a permutation of ``range(len(docs))``)."""
        ...
