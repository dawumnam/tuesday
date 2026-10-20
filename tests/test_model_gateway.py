"""Real model gateway, with the Gemini HTTP layer stubbed (no network)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from gemini_stub import GeminiStub
from pydantic import BaseModel

from task_design.costs import CostStore
from task_design.gateway import Message, Role
from task_design.model_gateway import InvalidModelOutput, build_gateway
from task_design.settings import ModelPrice, RoleConfig, Settings, ThinkingLevel


class Score(BaseModel):
    score: int
    reason: str


def gateway_for(settings: Settings, stub: GeminiStub, **kwargs):
    return build_gateway(settings, http_client=stub.client(), api_key="test-key", **kwargs)


def ask(text: str) -> list[Message]:
    return [Message(role="system", content="너는 채점 에이전트다."), Message(role="user", content=text)]


def test_each_role_calls_the_model_and_thinking_level_from_settings(settings):
    routed = settings.model_copy(
        update={
            "routing": settings.routing.model_copy(
                update={
                    "grading": RoleConfig(
                        model="grading-model", thinking=ThinkingLevel.LOW, max_output_tokens=1234
                    )
                }
            )
        }
    )
    stub = GeminiStub().reply('{"score": 80, "reason": "ok"}').reply("자유 응답")
    gateway = gateway_for(routed, stub)

    score = gateway.complete(Role.GRADING, ask("채점해라"), Score)
    text = gateway.complete(Role.EXAMINEE, ask("풀어라"))

    assert score == Score(score=80, reason="ok")
    assert text == "자유 응답"
    grading, examinee = stub.requests
    assert (grading.model, grading.thinking_level) == ("grading-model", "low")
    assert grading.generation_config["maxOutputTokens"] == 1234
    assert grading.generation_config["responseMimeType"] == "application/json"
    assert (examinee.model, examinee.thinking_level) == ("gemini-3.8-flash", "high")
    assert "responseMimeType" not in examinee.generation_config
    assert examinee.texts == ["너는 채점 에이전트다.", "풀어라"]


def test_invalid_structured_output_is_retried_with_the_error_fed_back(settings):
    stub = GeminiStub().reply('{"score": "높음"}').reply('{"score": 55, "reason": "fixed"}')
    gateway = gateway_for(settings, stub)

    result = gateway.complete(Role.REVIEW, ask("검토해라"), Score)

    assert result == Score(score=55, reason="fixed")
    first, retry = stub.requests
    assert retry.texts[: len(first.texts)] == first.texts
    feedback = retry.texts[len(first.texts) :]
    assert feedback[0] == '{"score": "높음"}'  # the model sees its own bad answer
    assert "score" in feedback[1] and "reason" in feedback[1]  # and what was wrong with it


def test_gives_up_after_the_configured_number_of_retries(settings):
    stub = GeminiStub()
    for _ in range(3):
        stub.reply("not json")
    gateway = gateway_for(settings.model_copy(update={"schema_retries": 1}), stub)

    with pytest.raises(InvalidModelOutput):
        gateway.complete(Role.GENERATION, ask("생성해라"), Score)

    assert len(stub.requests) == 2


def test_tokens_calls_and_cost_accumulate_per_draft_and_role(settings, db):
    priced = settings.model_copy(
        update={
            "prices": {"gemini-3.8-flash": ModelPrice(input_usd_per_mtok=1.0, output_usd_per_mtok=2.0)}
        }
    )
    stub = GeminiStub()
    stub.reply("a", input_tokens=1000, output_tokens=300, thoughts=700)  # draft, examinee
    stub.reply("b", input_tokens=2000, output_tokens=1000)  # draft, examinee
    stub.reply('{"score": 1}', input_tokens=500, output_tokens=10)  # draft, grading (invalid)
    stub.reply('{"score": 1, "reason": "r"}', input_tokens=600, output_tokens=20)  # retry
    stub.reply("c", input_tokens=9999, output_tokens=9999)  # another draft
    costs = CostStore(db)
    gateway = gateway_for(priced, stub, usage_sink=costs)
    draft, other = uuid4(), uuid4()

    gateway.complete(Role.EXAMINEE, ask("1"), draft_id=draft)
    gateway.complete(Role.EXAMINEE, ask("2"), draft_id=draft)
    gateway.complete(Role.GRADING, ask("3"), Score, draft_id=draft)
    gateway.complete(Role.EXAMINEE, ask("4"), draft_id=other)

    by_role = {c.role: c for c in costs.for_draft(draft)}
    assert set(by_role) == {"examinee", "grading"}
    examinee = by_role["examinee"]
    assert (examinee.calls, examinee.input_tokens, examinee.output_tokens) == (2, 3000, 2000)
    assert examinee.estimated_cost_usd == pytest.approx(0.003 + 0.004)
    grading = by_role["grading"]  # a schema retry is a billed call too
    assert (grading.calls, grading.input_tokens, grading.output_tokens) == (2, 1100, 30)
    assert grading.estimated_cost_usd == pytest.approx(0.00116)
    assert [c.calls for c in costs.for_draft(other)] == [1]


def test_embeddings_use_the_embedding_model_at_the_configured_dimension(settings, db):
    stub = GeminiStub()
    costs = CostStore(db)
    gateway = gateway_for(settings, stub, usage_sink=costs)
    draft = uuid4()

    vectors = gateway.embed(["상황 하나", "상황 둘"], draft_id=draft)

    (request,) = stub.requests
    assert (request.model, request.method) == ("gemini-embedding-2", "batchEmbedContents")
    assert len(vectors) == 2
    for v in vectors:
        assert len(v) == settings.embedding_dim
        assert sum(x * x for x in v) == pytest.approx(1.0)
    assert vectors[0] != vectors[1]
    (cost,) = costs.for_draft(draft)
    assert (cost.role, cost.calls) == ("embedding", 1)
    assert cost.input_tokens > 0


def test_rerank_asks_the_rerank_model_and_returns_its_order(settings):
    stub = GeminiStub().reply('{"order": [2, 0, 1]}')
    gateway = gateway_for(settings, stub)
    docs = ["회계 과제", "마케팅 과제", "재무 분석 과제"]

    assert gateway.rerank("재무 데이터 검증", docs) == [2, 0, 1]

    (request,) = stub.requests
    prompt = "\n".join(request.texts)
    assert "재무 데이터 검증" in prompt and all(d in prompt for d in docs)
    assert request.thinking_level == "high"


def test_rerank_always_returns_a_permutation_of_the_docs(settings):
    stub = GeminiStub().reply('{"order": [1, 1, 7]}')
    gateway = gateway_for(settings, stub)

    assert gateway.rerank("q", ["a", "b", "c"]) == [1, 0, 2]


def test_default_price_table_covers_the_default_models():
    s = Settings(_env_file=None)
    for role in Role:
        assert s.route(role).model in s.prices


def test_schema_retries_default_to_two():
    assert Settings(_env_file=None).schema_retries == 2
