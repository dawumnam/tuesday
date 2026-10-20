"""The real ``ModelGateway``: routes each role to a provider per settings.

Every provider call (including schema retries) is reported as a ``Usage`` to
the ``UsageSink`` with an estimated cost from ``settings.prices``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, ValidationError

from task_design.gateway import Message, Role, Usage, UsageSink
from task_design.providers import Completion, GeminiProvider, ProviderClient
from task_design.settings import Provider, RoleConfig, Settings


class InvalidModelOutput(Exception):
    """The model kept returning output that does not validate against the schema."""

    def __init__(self, role: Role, schema: type[BaseModel], error: ValidationError | None) -> None:
        super().__init__(f"{role} output never validated as {schema.__name__}: {error}")
        self.role = role
        self.schema = schema
        self.error = error


def _retry_prompt(error: ValidationError) -> str:
    problems = "\n".join(
        f"- {'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in error.errors()
    )
    return (
        "직전 응답이 요구한 JSON 스키마를 만족하지 않습니다. 아래 문제를 고쳐 "
        f"JSON만 다시 출력하세요.\nThe previous output failed validation:\n{problems}"
    )


class RoutedGateway:
    def __init__(
        self,
        settings: Settings,
        providers: Mapping[Provider, ProviderClient],
        usage_sink: UsageSink | None = None,
    ) -> None:
        self.settings = settings
        self.providers = dict(providers)
        self.usage_sink = usage_sink

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

    def _provider(self, role: Role) -> tuple[RoleConfig, ProviderClient]:
        config = self.settings.route(role)
        provider = self.providers.get(config.provider)
        if provider is None:
            raise LookupError(f"no {config.provider} provider configured for role {role}")
        return config, provider

    def _generate(
        self,
        role: Role,
        messages: Sequence[Message],
        json_schema: dict[str, Any] | None,
        draft_id: UUID | None,
    ) -> Completion:
        config, provider = self._provider(role)
        completion = provider.generate(config, messages, json_schema)
        self._record(role, draft_id, provider, config, completion.input_tokens, completion.output_tokens)
        return completion

    def _record(
        self,
        role: Role,
        draft_id: UUID | None,
        provider: ProviderClient,
        config: RoleConfig,
        input_tokens: int,
        output_tokens: int,
    ) -> Usage:
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
        return usage


def build_gateway(
    settings: Settings,
    *,
    usage_sink: UsageSink | None = None,
    api_key: str | None = None,
    http_client: httpx.Client | None = None,
) -> RoutedGateway:
    gemini = GeminiProvider(api_key=api_key or settings.gemini_api_key, http_client=http_client)
    return RoutedGateway(settings, {Provider.GEMINI: gemini}, usage_sink=usage_sink)
