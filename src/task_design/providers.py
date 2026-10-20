"""Provider adapters behind the model gateway.

A provider turns one role's ``RoleConfig`` plus messages into a model call and
reports token counts. ``Provider.LOCAL`` (ticket 12) adds an OpenAI-compatible
adapter with the same shape.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from google import genai
from google.genai import types

from task_design.gateway import Message, Vector
from task_design.settings import RoleConfig, ThinkingLevel


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int  # includes thinking tokens (billed as output)


@dataclass(frozen=True)
class Embeddings:
    vectors: list[Vector]
    input_tokens: int


class ProviderClient(Protocol):
    name: str

    def generate(
        self, config: RoleConfig, messages: Sequence[Message], json_schema: dict[str, Any] | None
    ) -> Completion: ...

    def embed(self, config: RoleConfig, texts: Sequence[str], dim: int) -> Embeddings: ...


_THINKING = {
    ThinkingLevel.LOW: types.ThinkingLevel.LOW,
    ThinkingLevel.MEDIUM: types.ThinkingLevel.MEDIUM,
    ThinkingLevel.HIGH: types.ThinkingLevel.HIGH,
}


def estimate_tokens(text: str) -> int:
    """Rough count for calls whose API reports no usage (Gemini embeddings)."""
    return max(1, math.ceil(len(text) / 3))


def _normalize(values: Sequence[float]) -> Vector:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values] if norm else list(values)


class GeminiProvider:
    """google-genai ``generateContent`` / ``batchEmbedContents``."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, http_client: httpx.Client | None = None) -> None:
        http_options = types.HttpOptions(httpx_client=http_client) if http_client else None
        self._client = genai.Client(api_key=api_key, http_options=http_options)

    def generate(
        self, config: RoleConfig, messages: Sequence[Message], json_schema: dict[str, Any] | None
    ) -> Completion:
        system = "\n\n".join(m.content for m in messages if m.role == "system") or None
        contents = [
            types.Content(
                role="model" if m.role == "assistant" else "user",
                parts=[types.Part(text=m.content)],
            )
            for m in messages
            if m.role != "system"
        ]
        gen_config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=config.max_output_tokens or None,
            thinking_config=(
                types.ThinkingConfig(thinking_level=_THINKING[config.thinking])
                if config.thinking in _THINKING
                else None
            ),
            response_mime_type="application/json" if json_schema is not None else None,
            response_json_schema=json_schema,
        )
        response = self._client.models.generate_content(
            model=config.model, contents=contents, config=gen_config
        )
        usage = response.usage_metadata
        input_tokens = (usage.prompt_token_count or 0) if usage else 0
        output_tokens = (
            ((usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)) if usage else 0
        )
        return Completion(text=response.text or "", input_tokens=input_tokens, output_tokens=output_tokens)

    def embed(self, config: RoleConfig, texts: Sequence[str], dim: int) -> Embeddings:
        # One Content per text: for gemini-embedding-2 the SDK folds a bare list of
        # strings into a single multi-part Content, which yields ONE aggregated vector.
        response = self._client.models.embed_content(
            model=config.model,
            contents=[types.Content(role="user", parts=[types.Part(text=t)]) for t in texts],
            config=types.EmbedContentConfig(output_dimensionality=dim),
        )
        vectors = [_normalize(e.values or []) for e in response.embeddings or []]
        # The Gemini API returns no token usage for embeddings; estimate it.
        return Embeddings(vectors=vectors, input_tokens=sum(estimate_tokens(t) for t in texts))
