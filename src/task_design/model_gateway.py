"""The real ``ModelGateway``: routes each role to a provider per settings.

Every provider call (including schema retries) is reported as a ``Usage`` to
the ``UsageSink`` with an estimated cost from ``settings.prices``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from task_design.gateway import Message, Role, Usage, UsageSink, Vector
from task_design.providers import Completion, GeminiProvider, ProviderClient
from task_design.settings import Provider, RoleConfig, Settings
from task_design.tracing import CallRecord, NoopTracer, Tracer, current_scope, make_tracer


class InvalidModelOutput(Exception):
    """The model kept returning output that does not validate against the schema."""

    def __init__(self, role: Role, schema: type[BaseModel], error: ValidationError | None) -> None:
        super().__init__(f"{role} output never validated as {schema.__name__}: {error}")
        self.role = role
        self.schema = schema
        self.error = error


class ProviderNotConfigured(LookupError):
    """No provider client is configured for the role's ``RoleConfig.provider``."""


def _retry_prompt(error: ValidationError) -> str:
    problems = "\n".join(
        f"- {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in error.errors()
    )
    return (
        "직전 응답이 요구한 JSON 스키마를 만족하지 않습니다. 아래 문제를 고쳐 "
        f"JSON만 다시 출력하세요.\nThe previous output failed validation:\n{problems}"
    )


class _Ranking(BaseModel):
    order: list[int]


_RERANK_SYSTEM = (
    "너는 검색 결과 리랭커다. 질의와 번호가 붙은 문서 목록을 받으면, 질의와의 관련성이 "
    "높은 순서대로 모든 문서 번호를 order 배열에 담아 JSON으로만 답한다."
)


class RoutedGateway:
    def __init__(
        self,
        settings: Settings,
        providers: Mapping[Provider, ProviderClient],
        usage_sink: UsageSink | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self.settings = settings
        self.providers = dict(providers)
        self.usage_sink = usage_sink
        self.tracer: Tracer = tracer if tracer is not None else NoopTracer()

    def complete(
        self,
        role: Role,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        draft_id: UUID | None = None,
    ):
        if schema is None:
            return self._generate(role, messages, None, draft_id).text

        json_schema = schema.model_json_schema()
        conversation = list(messages)
        error: ValidationError | None = None
        for _ in range(self.settings.schema_retries + 1):
            completion = self._generate(role, conversation, json_schema, draft_id)
            try:
                return schema.model_validate_json(completion.text)
            except ValidationError as exc:
                error = exc
                conversation += [
                    Message(role="assistant", content=completion.text),
                    Message(role="user", content=_retry_prompt(exc)),
                ]
        raise InvalidModelOutput(role, schema, error)

    def embed(self, texts: Sequence[str], draft_id: UUID | None = None) -> list[Vector]:
        if not texts:
            return []
        config, provider = self._provider(Role.EMBEDDING)

        def call() -> tuple[list[Vector], str, int, int]:
            result = provider.embed(config, texts, self.settings.embedding_dim)
            summary = f"{len(result.vectors)} vectors × {self.settings.embedding_dim}"
            return result.vectors, summary, result.input_tokens, 0

        return self._observed(Role.EMBEDDING, draft_id, config, provider, list(texts), call)

    def rerank(self, query: str, docs: Sequence[str], draft_id: UUID | None = None) -> list[int]:
        if len(docs) <= 1:
            return list(range(len(docs)))
        listing = "\n\n".join(f"[{i}]\n{doc}" for i, doc in enumerate(docs))
        messages = [
            Message(role="system", content=_RERANK_SYSTEM),
            Message(role="user", content=f"질의:\n{query}\n\n문서:\n{listing}"),
        ]
        ranking = self.complete(Role.RERANK, messages, _Ranking, draft_id=draft_id)
        seen: dict[int, None] = {}
        for i in ranking.order:
            if 0 <= i < len(docs):
                seen.setdefault(i)
        return list(seen) + [i for i in range(len(docs)) if i not in seen]

    def _provider(self, role: Role) -> tuple[RoleConfig, ProviderClient]:
        config = self.settings.route(role)
        provider = self.providers.get(config.provider)
        if provider is None:
            raise ProviderNotConfigured(f"{role} 역할에 설정된 공급자({config.provider})가 없습니다.")
        return config, provider

    def _generate(
        self,
        role: Role,
        messages: Sequence[Message],
        json_schema: dict[str, Any] | None,
        draft_id: UUID | None,
    ) -> Completion:
        config, provider = self._provider(role)

        def call() -> tuple[Completion, str, int, int]:
            c = provider.generate(config, messages, json_schema)
            return c, c.text, c.input_tokens, c.output_tokens

        return self._observed(role, draft_id, config, provider, list(messages), call)

    def _observed[T](
        self,
        role: Role,
        draft_id: UUID | None,
        config: RoleConfig,
        provider: ProviderClient,
        call_input: list[Message] | list[str],
        call: Callable[[], tuple[T, str, int, int]],
    ) -> T:
        """Run one provider call; report its usage and trace it (also on failure)."""
        start = datetime.now(UTC)
        try:
            value, output, input_tokens, output_tokens = call()
        except Exception as exc:
            self.tracer.record(
                self._call_record(role, draft_id, config, provider, call_input, start, error=exc)
            )
            raise
        price = self.settings.prices.get(config.model)
        usage = Usage(
            role=role,
            draft_id=draft_id,
            provider=provider.name,
            model=config.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=price.cost(input_tokens, output_tokens) if price else 0.0,
        )
        if self.usage_sink is not None:
            self.usage_sink.record(usage)
        self.tracer.record(
            self._call_record(
                role, draft_id, config, provider, call_input, start, output=output, usage=usage
            )
        )
        return value

    @staticmethod
    def _call_record(
        role: Role,
        draft_id: UUID | None,
        config: RoleConfig,
        provider: ProviderClient,
        call_input: list[Message] | list[str],
        start: datetime,
        output: str | None = None,
        usage: Usage | None = None,
        error: Exception | None = None,
    ) -> CallRecord:
        return CallRecord(
            role=role,
            draft_id=draft_id,
            provider=provider.name,
            model=config.model,
            input=call_input,
            output=output,
            start=start,
            end=datetime.now(UTC),
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cost_usd=usage.estimated_cost_usd if usage else 0.0,
            error=f"{type(error).__name__}: {error}" if error else None,
            parameters={"thinking": config.thinking.value, "max_output_tokens": config.max_output_tokens},
            scope=current_scope(),
        )


def build_gateway(
    settings: Settings,
    *,
    usage_sink: UsageSink | None = None,
    tracer: Tracer | None = None,
    api_key: str | None = None,
    http_client: httpx.Client | None = None,
) -> RoutedGateway:
    """The production gateway: Gemini for every role (ticket 12 adds ``Provider.LOCAL``).

    ``usage_sink`` is normally ``CostStore(db)``; ``tracer`` defaults to
    ``make_tracer(settings)`` (Langfuse, or a no-op without keys).
    """
    gemini = GeminiProvider(api_key=api_key or settings.gemini_api_key, http_client=http_client)
    return RoutedGateway(
        settings,
        {Provider.GEMINI: gemini},
        usage_sink=usage_sink,
        tracer=tracer if tracer is not None else make_tracer(settings),
    )
