"""Stub of the Gemini REST API at the HTTP layer (httpx MockTransport).

Records each request and answers from a FIFO of scripted replies, so gateway
tests exercise the real google-genai client without touching the network.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class StubRequest:
    model: str
    method: str  # "generateContent" | "batchEmbedContents"
    body: dict[str, Any]

    @property
    def generation_config(self) -> dict[str, Any]:
        return self.body.get("generationConfig", {})

    @property
    def thinking_level(self) -> str | None:
        cfg = self.generation_config.get("thinkingConfig", {})
        level = cfg.get("thinkingLevel", cfg.get("thinking_level"))
        return level.lower() if level else None

    @property
    def texts(self) -> list[str]:
        """Every text part of the request, system instruction first."""
        out = [p["text"] for p in self.body.get("systemInstruction", {}).get("parts", [])]
        for content in self.body.get("contents", []):
            out += [p["text"] for p in content.get("parts", []) if "text" in p]
        return out


class GeminiStub:
    def __init__(self) -> None:
        self.requests: list[StubRequest] = []
        self._replies: deque[tuple[str, int, int, int]] = deque()

    def reply(self, text: str, input_tokens: int = 10, output_tokens: int = 5, thoughts: int = 0):
        self._replies.append((text, input_tokens, output_tokens, thoughts))
        return self

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/", 1)[-1]
        model, method = path.split(":")
        body = json.loads(request.content or b"{}")
        self.requests.append(StubRequest(model=model, method=method, body=body))
        if method == "batchEmbedContents":
            return httpx.Response(200, json={"embeddings": self._embeddings(body)})
        if not self._replies:
            raise AssertionError(f"unscripted generateContent call to {model}")
        text, prompt_tokens, out_tokens, thoughts = self._replies.popleft()
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {
                    "promptTokenCount": prompt_tokens,
                    "candidatesTokenCount": out_tokens,
                    "thoughtsTokenCount": thoughts,
                },
            },
        )

    @staticmethod
    def _embeddings(body: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for i, req in enumerate(body["requests"]):
            dim = req.get("outputDimensionality", 3072)
            values = [0.0] * dim
            values[i % dim] = 2.0  # deliberately not unit length
            out.append({"values": values})
        return out
