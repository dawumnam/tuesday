"""TaskRepository against real Postgres with controlled FakeGateway vectors.

Each test starts from empty task/competency tables (``empty_repositories``)
and uses its own random direction in embedding space.
"""

from __future__ import annotations

import math
import random
import uuid

import pytest

from task_design.domain import Audience, Level, Rubric, RubricItem, RubricItemType, Task
from task_design.gateway import Role
from task_design.repositories import TaskRepository


def random_unit(dim: int) -> list[float]:
    rng = random.Random(uuid.uuid4().int)
    v = [rng.gauss(0, 1) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def with_cosine(v: list[float], cosine: float) -> list[float]:
    """A unit vector whose cosine similarity with unit vector ``v`` is ``cosine``."""
    w = random_unit(len(v))
    dot = sum(a * b for a, b in zip(v, w, strict=True))
    w = [b - dot * a for a, b in zip(v, w, strict=True)]  # orthogonal to v
    n = math.sqrt(sum(x * x for x in w))
    s = math.sqrt(1 - cosine * cosine)
    return [cosine * a + s * b / n for a, b in zip(v, w, strict=True)]


def make_task(scenario: str, instruction: str = "보고서를 작성하라.") -> Task:
    item = dict(competency_id="C1", criteria_zero="없음", criteria_partial="일부", criteria_full="충분")
    return Task(
        scenario=scenario,
        instruction=instruction,
        rubric=Rubric(
            items=[
                RubricItem(title="검증", type=RubricItemType.PROCESS, points=30, **item),
                RubricItem(title="정확성", type=RubricItemType.OUTPUT, points=40, **item),
                RubricItem(title="구성", type=RubricItemType.OUTPUT, points=30, **item),
            ]
        ),
        competency_ids=["C1"],
        audience=Audience(job="영업", career_level="신입"),
        difficulty=Level.MEDIUM,
    )


@pytest.fixture
def repo(empty_repositories, fake_gateway, settings) -> TaskRepository:
    return TaskRepository(empty_repositories, fake_gateway, settings)


@pytest.fixture
def dim(settings) -> int:
    return settings.embedding_dim


def test_keyword_only_match_is_found(repo, fake_gateway, dim):
    query = "냉동창고 온도기록 이상치"
    q = random_unit(dim)
    fake_gateway.set_embedding(query, q)

    # Semantically close distractors with unrelated wording.
    for i in range(3):
        text = f"분기별 마케팅 예산 집행 현황 {i} {uuid.uuid4().hex}"
        fake_gateway.set_embedding(text, with_cosine(q, 0.95))
        repo.add(make_task(text))

    # Keyword match whose embedding is orthogonal to the query.
    keyword_text = f"물류센터 냉동창고 온도기록에서 이상치를 찾는 상황 {uuid.uuid4().hex}"
    fake_gateway.set_embedding(keyword_text, with_cosine(q, 0.0))
    keyword_id = repo.add(make_task(keyword_text))

    hits = repo.hybrid_search(query, k=5)

    assert keyword_id in [t.id for t in hits]


def test_semantic_only_match_is_found(repo, fake_gateway, dim):
    query = "고객 불만 응대 이메일 초안 다듬기"
    q = random_unit(dim)
    fake_gateway.set_embedding(query, q)

    # Wording overlaps the query, meaning does not.
    for i in range(3):
        text = f"고객 불만 응대 이메일 초안 다듬기 교육 일정표 정리 {i} {uuid.uuid4().hex}"
        fake_gateway.set_embedding(text, with_cosine(q, 0.0))
        repo.add(make_task(text))

    # Same meaning in entirely different wording.
    semantic_text = f"클레임을 제기한 거래처에 보낼 회신문을 AI로 손보는 상황 {uuid.uuid4().hex}"
    fake_gateway.set_embedding(semantic_text, with_cosine(q, 0.97))
    semantic_id = repo.add(make_task(semantic_text, instruction="회신문을 완성하라."))

    hits = repo.hybrid_search(query, k=5)

    assert semantic_id in [t.id for t in hits]


def test_tasks_without_keyword_overlap_do_not_crowd_out_a_semantic_match(repo, fake_gateway, dim):
    query = "신제품 출시 보도자료 검토"
    q = random_unit(dim)
    fake_gateway.set_embedding(query, q)

    # Keyword matches with unrelated meaning.
    for i in range(3):
        text = f"신제품 출시 보도자료 검토 회의실 예약 {i} {uuid.uuid4().hex}"
        fake_gateway.set_embedding(text, with_cosine(q, 0.0))
        repo.add(make_task(text))

    # Tasks sharing no wording with the query and only faintly related in
    # meaning. Their ids sort first, so they win every keyword-rank tie.
    for i in range(25):
        text = f"무관한 과제 {i} {uuid.uuid4().hex}"
        fake_gateway.set_embedding(text, with_cosine(q, 0.2))
        repo.add(make_task(text).model_copy(update={"id": uuid.UUID(int=i + 1)}))

    semantic_text = f"새 상품을 알리는 언론 배포문을 AI로 손보는 상황 {uuid.uuid4().hex}"
    fake_gateway.set_embedding(semantic_text, with_cosine(q, 0.97))
    semantic_id = repo.add(
        make_task(semantic_text, instruction="배포문을 완성하라.").model_copy(
            update={"id": uuid.UUID("ffffffff-ffff-4fff-bfff-ffffffffffff")}
        )
    )

    hits = repo.hybrid_search(query, k=5)

    assert semantic_id in [t.id for t in hits]


def test_rerank_order_is_applied(repo, fake_gateway, dim):
    query = "주간 영업 실적 대시보드 요약"
    q = random_unit(dim)
    fake_gateway.set_embedding(query, q)

    best_text = f"주간 영업 실적 대시보드를 요약하는 상황 {uuid.uuid4().hex}"
    fake_gateway.set_embedding(best_text, with_cosine(q, 0.99))
    best_id = repo.add(make_task(best_text))

    favoured_text = f"영업 실적 회의록을 정리하는 상황 [리랭커선호] {uuid.uuid4().hex}"
    fake_gateway.set_embedding(favoured_text, with_cosine(q, 0.9))
    favoured_id = repo.add(make_task(favoured_text))

    def favour_marked(call):
        marked = [i for i, d in enumerate(call.docs) if "[리랭커선호]" in d and favoured_text in d]
        rest = [i for i in range(len(call.docs)) if i not in marked]
        return marked + rest

    fake_gateway.respond_with(Role.RERANK, favour_marked)

    hits = repo.hybrid_search(query, k=2)

    assert [t.id for t in hits] == [favoured_id, best_id]
    [rerank_call] = fake_gateway.calls_for(Role.RERANK)
    assert rerank_call.query == query


def test_duplicate_threshold_is_respected(repo, fake_gateway, dim, settings):
    assert settings.duplicate_threshold == 0.85
    existing_text = f"신규 입사자 온보딩 체크리스트를 만드는 상황 {uuid.uuid4().hex}"
    v = random_unit(dim)
    fake_gateway.set_embedding(existing_text, v)
    existing_id = repo.add(make_task(existing_text))

    above = "온보딩 체크리스트 후보 A"
    below = "온보딩 체크리스트 후보 B"
    fake_gateway.set_embedding(above, with_cosine(v, 0.88))
    fake_gateway.set_embedding(below, with_cosine(v, 0.82))

    [match] = repo.find_duplicates(above)
    assert match.task.id == existing_id
    assert match.similarity == pytest.approx(0.88, abs=1e-3)
    assert repo.find_duplicates(below) == []

    # An explicit threshold overrides the setting.
    assert [m.task.id for m in repo.find_duplicates(below, threshold=0.8)] == [existing_id]
