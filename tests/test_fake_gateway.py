"""FakeGateway is shared test infrastructure; pin its scripting contract."""

import math
from uuid import uuid4

import pytest
from pydantic import BaseModel

from task_design.gateway import Message, ModelGateway, Role
from task_design.testing.fake_gateway import FakeGateway, FakeResponse


class Answer(BaseModel):
    text: str


def msgs(content: str) -> list[Message]:
    return [Message(role="user", content=content)]


def test_is_a_model_gateway():
    assert isinstance(FakeGateway(), ModelGateway)


def test_scripted_responses_come_back_in_order_validated_against_schema():
    fake = FakeGateway().script(Role.GENERATION, Answer(text="첫째"), {"text": "둘째"})
    assert fake.complete(Role.GENERATION, msgs("a"), Answer) == Answer(text="첫째")
    assert fake.complete(Role.GENERATION, msgs("b"), Answer) == Answer(text="둘째")
    with pytest.raises(AssertionError):
        fake.complete(Role.GENERATION, msgs("c"), Answer)


def test_callable_responder_sees_the_call():
    fake = FakeGateway().respond_with(
        Role.EXAMINEE, lambda call: "상 수준" if "상" in call.prompt else "하 수준"
    )
    assert fake.complete(Role.EXAMINEE, msgs("당신은 상 수준")) == "상 수준"
    assert fake.complete(Role.EXAMINEE, msgs("당신은 하 수준")) == "하 수준"


def test_records_calls_and_token_usage_per_draft():
    draft = uuid4()
    fake = FakeGateway().script(
        Role.GRADING, FakeResponse({"text": "x"}, input_tokens=100, output_tokens=20)
    )
    fake.complete(Role.GRADING, msgs("채점"), Answer, draft_id=draft)
    fake.embed(["가"], draft_id=uuid4())
    assert [c.role for c in fake.calls] == [Role.GRADING, Role.EMBEDDING]
    assert fake.total_tokens(draft) == 120


def test_usage_is_forwarded_to_a_sink():
    seen = []

    class Sink:
        def record(self, usage):
            seen.append(usage)

    FakeGateway(usage_sink=Sink()).embed(["가", "나"])
    assert [u.role for u in seen] == [Role.EMBEDDING]


def test_embeddings_are_deterministic_and_overridable():
    fake = FakeGateway(embedding_dim=4).set_embedding("고정", [1.0, 0.0, 0.0, 0.0])
    a1, b, fixed = fake.embed(["가", "나", "고정"])
    (a2,) = fake.embed(["가"])
    assert a1 == a2 and a1 != b
    assert math.isclose(sum(x * x for x in a1), 1.0)
    assert fixed == [1.0, 0.0, 0.0, 0.0]


def test_rerank_defaults_to_input_order_and_can_be_scripted():
    fake = FakeGateway()
    assert fake.rerank("q", ["a", "b", "c"]) == [0, 1, 2]
    fake.script(Role.RERANK, [2, 0, 1])
    assert fake.rerank("q", ["a", "b", "c"]) == [2, 0, 1]
