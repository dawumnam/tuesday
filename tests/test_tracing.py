"""Langfuse tracing: one trace per draft, one generation per model call."""

from __future__ import annotations

import gzip
import json
from uuid import uuid4

import httpx
from gemini_stub import GeminiStub
from pydantic import BaseModel

from task_design.gateway import Message, Role
from task_design.model_gateway import build_gateway
from task_design.tracing import LangfuseTracer, NoopTracer, make_tracer


class Answer(BaseModel):
    answer: str


class LangfuseStub:
    """Collects ingestion events posted to a Langfuse v2 server."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        raw = request.content
        if request.headers.get("content-encoding") == "gzip":
            raw = gzip.decompress(raw)
        if request.url.path.endswith("/api/public/ingestion"):
            self.events += json.loads(raw)["batch"]
        return httpx.Response(207, json={"successes": [], "errors": []})

    def of_type(self, kind: str) -> list[dict]:
        return [e["body"] for e in self.events if e["type"] == kind]


def test_tracing_is_a_no_op_without_langfuse_keys(settings):
    assert isinstance(make_tracer(settings), NoopTracer)


def test_each_call_of_a_draft_is_a_generation_under_that_drafts_trace(settings):
    langfuse = LangfuseStub()
    tracer = LangfuseTracer(
        public_key="pk", secret_key="sk", host="http://langfuse.test", httpx_client=langfuse.client()
    )
    stub = GeminiStub()
    stub.reply('{"answer": "하나"}', input_tokens=11, output_tokens=3, thoughts=4)
    stub.reply("둘")
    gateway = build_gateway(
        settings, http_client=stub.client(), api_key="k", tracer=tracer
    )
    draft = uuid4()

    gateway.complete(Role.GENERATION, [Message(role="user", content="첫 질문")], Answer, draft_id=draft)
    gateway.complete(Role.EXAMINEE, [Message(role="user", content="두번째")], draft_id=draft)
    tracer.flush()

    assert {t["id"] for t in langfuse.of_type("trace-create")} == {str(draft)}
    generations = langfuse.of_type("generation-create")
    assert [g["traceId"] for g in generations] == [str(draft)] * 2
    first, second = generations
    assert (first["name"], first["model"]) == ("generation", "gemini-3.8-flash")
    assert "첫 질문" in json.dumps(first["input"], ensure_ascii=False)
    assert first["output"] == '{"answer": "하나"}'
    assert (first["usage"]["input"], first["usage"]["output"]) == (11, 7)
    assert first["usage"]["totalCost"] > 0
    assert first["startTime"] <= first["endTime"]
    assert second["name"] == "examinee"
