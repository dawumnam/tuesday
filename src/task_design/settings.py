"""Application settings (pydantic-settings).

Every field is overridable via environment variables with the ``TD_`` prefix;
nested fields use ``__``. Examples::

    TD_DATABASE_URL=postgresql://...
    TD_SOLVABLE_MIN_SCORE=65
    TD_ROUTING__GRADING__MODEL=some-other-model
    TD_ROUTING__GRADING__THINKING=low
    TD_USE_LOCAL_LLM=true
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from task_design.gateway import Role

# Confirmed against Google's docs on 2026-10-20: see docs/research/model-ids.md.
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_GEMINI_EMBEDDING_MODEL = "gemini-embedding-2"


class ModelPrice(BaseModel):
    """USD per 1M tokens. Output includes thinking tokens (Gemini bills them as output)."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float = 0.0

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_usd_per_mtok + output_tokens * self.output_usd_per_mtok
        ) / 1_000_000


# Gemini API paid tier, text input, as listed on 2026-10-20. gemini-3.8-flash
# doubles to 1.50 / 7.50 from 2027-01-01 (override with TD_PRICES).
DEFAULT_PRICES = {
    DEFAULT_GEMINI_MODEL: ModelPrice(input_usd_per_mtok=0.75, output_usd_per_mtok=3.75),
    DEFAULT_GEMINI_EMBEDDING_MODEL: ModelPrice(input_usd_per_mtok=0.20),
}


class Provider(StrEnum):
    GEMINI = "gemini"
    # Deferred (ticket 12): OpenAI-compatible local endpoint on the `ml` server.
    LOCAL = "local"


class ThinkingLevel(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RoleConfig(BaseModel):
    provider: Provider = Provider.GEMINI
    model: str = DEFAULT_GEMINI_MODEL
    thinking: ThinkingLevel = ThinkingLevel.HIGH
    max_output_tokens: int = 8192


class Routing(BaseModel):
    """Role → provider/model/thinking/token budget. One field per ``Role``."""

    generation: RoleConfig = RoleConfig()
    review: RoleConfig = RoleConfig()
    examinee: RoleConfig = RoleConfig()
    ai_tool: RoleConfig = RoleConfig()
    grading: RoleConfig = RoleConfig()
    rerank: RoleConfig = RoleConfig()
    embedding: RoleConfig = RoleConfig(
        model=DEFAULT_GEMINI_EMBEDDING_MODEL, thinking=ThinkingLevel.NONE, max_output_tokens=0
    )

    def for_role(self, role: Role) -> RoleConfig:
        return getattr(self, role.value)


# Roles that `use_local_llm` moves to the local endpoint (ADR 0003; deferred to ticket 12).
LOCAL_LLM_ROLES = frozenset({Role.EXAMINEE, Role.AI_TOOL})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TD_", env_nested_delimiter="__", env_file=".env", extra="ignore"
    )

    database_url: str = "postgresql://task_design:task_design@localhost:54329/task_design"

    # 판정 기준값 (verdict thresholds)
    solvable_min_score: float = 70.0  # 풀이 가능성: 상 persona score ≥ this
    discrimination_min_gap: float = 30.0  # 변별력: 상 − 하 ≥ this (and 상 > 중 > 하)
    grading_max_stdev: float = 5.0  # 채점 일관성: stdev of 3 gradings ≤ this
    gradings_per_submission: int = 3

    duplicate_threshold: float = 0.85  # embedding cosine ≥ this ⇒ 중복
    similar_tasks_k: int = 5  # 유사 과제 shown to the 상황 후보 generator
    candidate_generation_attempts: int = 3  # generation calls per batch of 3 상황 후보
    # 자기 수정 루프: at most this many fixes after round 1 (so up to max_fix_rounds + 1 rounds)
    max_fix_rounds: int = 3
    draft_token_budget: int = 2_000_000  # total tokens per draft before the loop stops
    max_examinee_turns: int = 6

    embedding_dim: int = 768

    # 성공 기준 (dashboard targets)
    pass_ratio_target: float = 0.70  # 전면 재작성 없이 통과한 비율 ≥ this
    work_minutes_target: float = 42.0  # 과제 1개당 총 작업 시간 ≤ this

    routing: Routing = Routing()
    use_local_llm: bool = False
    local_llm_url: str | None = None  # placeholder; ticket 12
    local_llm_model: str | None = None  # placeholder; ticket 12

    schema_retries: int = 2  # extra attempts when output fails schema validation
    # model id → price; unknown models are recorded at cost 0.
    prices: dict[str, ModelPrice] = dict(DEFAULT_PRICES)
    gemini_api_key: str | None = None  # TD_GEMINI_API_KEY; falls back to GEMINI_API_KEY

    # 데모 모드 (TD_FAKE_GATEWAY=1) only: how a new draft's run proceeds on the 진행 screen.
    # "step" = 단계별 진행 (pauses before every stage), "auto" = 자동 진행.
    demo_step_mode: Literal["step", "auto"] = "step"

    langfuse_host: str = "http://localhost:3300"
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    # Langfuse project id (from the project URL), for per-draft trace links in metrics.
    langfuse_project_id: str | None = None
    # Langfuse address as the *browser* sees it, for trace links (TD_LANGFUSE_PUBLIC_HOST).
    # Defaults to langfuse_host; set it when the app reaches Langfuse on an internal host
    # (compose: langfuse_host=http://langfuse:3000, public host http://localhost:3300).
    langfuse_public_host: str | None = None

    def route(self, role: Role) -> RoleConfig:
        """Effective config for a role, after applying the `use_local_llm` switch."""
        config = self.routing.for_role(role)
        if self.use_local_llm and role in LOCAL_LLM_ROLES:
            return config.model_copy(
                update={"provider": Provider.LOCAL, "model": self.local_llm_model or config.model}
            )
        return config
