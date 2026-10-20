"""Langfuse tracing: one trace per graph execution, grouped by draft as a session;
one generation per model call, named by the workflow node that made it."""

from __future__ import annotations

import gzip
import json
from uuid import uuid4

import httpx
from gemini_stub import GeminiStub
from pydantic import BaseModel

from task_design.gateway import Message, Role
from task_design.model_gateway import build_gateway
from task_design.service import TaskDesignService
from task_design.testing.workflow import (
    Personas,
    ensure_competencies,
    make_input,
    start_awaiting_review,
)
from task_design.tracing import LangfuseTracer, NoopTracer, current_scope, make_tracer


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


def _tracer(langfuse: LangfuseStub) -> LangfuseTracer:
    return LangfuseTracer(
        public_key="pk", secret_key="sk", host="http://langfuse.test", httpx_client=langfuse.client()
    )


def _candidates(tag: str) -> str:
    return json.dumps({"candidates": [
        {"title": f"{tag} {i}", "scenario": f"{tag} 상황 {i}: 업무 자료를 AI로 점검한다",
         "referenced_task_ids": []} for i in range(3)]}, ensure_ascii=False)


def test_each_graph_execution_is_one_trace_in_the_drafts_session(empty_repositories, settings):
    db = empty_repositories
    ensure_competencies(db)
    langfuse = LangfuseStub()
    tracer = _tracer(langfuse)
    stub = GeminiStub().reply(_candidates("첫 후보")).reply(_candidates("새 후보"))
    gateway = build_gateway(settings, http_client=stub.client(), api_key="k", tracer=tracer)
    service = TaskDesignService(db, gateway, settings)

    draft = service.start(make_input(), author_id=1)
    service.regenerate_scenarios(draft.id)
    tracer.flush()

    traces = langfuse.of_type("trace-create")
    assert [(t["name"], t["sessionId"]) for t in traces] == [
        ("start", str(draft.id)), ("regenerate", str(draft.id))]
    start, regenerate = (t["id"] for t in traces)
    assert start != regenerate
    generations = langfuse.of_type("generation-create")
    names = {trace: [g["name"] for g in generations if g["traceId"] == trace]
             for trace in (start, regenerate)}
    assert names[start] == ["search_similar_tasks", "generate_candidates", "generate_candidates"]
    assert names[regenerate] == ["generate_candidates", "generate_candidates"]
    assert all(g["metadata"]["role"] for g in generations)


def test_a_model_call_outside_a_graph_execution_is_still_in_the_drafts_session(settings):
    langfuse = LangfuseStub()
    tracer = _tracer(langfuse)
    stub = GeminiStub()
    stub.reply('{"answer": "하나"}', input_tokens=11, output_tokens=3, thoughts=4)
    gateway = build_gateway(settings, http_client=stub.client(), api_key="k", tracer=tracer)
    draft = uuid4()

    gateway.complete(Role.GENERATION, [Message(role="user", content="첫 질문")], Answer, draft_id=draft)
    tracer.flush()

    [trace] = langfuse.of_type("trace-create")
    assert trace["sessionId"] == str(draft)
    [generation] = langfuse.of_type("generation-create")
    assert generation["traceId"] == trace["id"]
    assert (generation["name"], generation["model"]) == ("generation", "gemini-3.8-flash")
    assert "첫 질문" in json.dumps(generation["input"], ensure_ascii=False)
    assert generation["output"] == '{"answer": "하나"}'
    assert (generation["usage"]["input"], generation["usage"]["output"]) == (11, 7)
    assert generation["usage"]["totalCost"] > 0
    assert generation["startTime"] <= generation["endTime"]


def test_trace_links_use_the_public_host_when_the_app_reaches_langfuse_internally():
    from task_design.settings import Settings
    from task_design.tracing import trace_url

    draft_id = uuid4()
    settings = Settings(_env_file=None, langfuse_host="http://langfuse:3000",
                        langfuse_public_host="http://localhost:3300",
                        langfuse_project_id="task-design")

    assert trace_url(settings, draft_id) == (
        f"http://localhost:3300/project/task-design/sessions/{draft_id}"
    )


def test_calls_from_parallel_nodes_stay_in_their_executions_trace(
    service, fake_gateway, db, settings
):
    scopes = []
    personas = Personas(before_call=lambda level, call: scopes.append(current_scope()))

    start_awaiting_review(service, fake_gateway, db, settings, personas=personas)

    assert {(s.run, s.node) for s in scopes} == {("select", "run_persona")}
    assert len({s.trace_id for s in scopes}) == 1
