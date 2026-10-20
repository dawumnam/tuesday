"""데모 모드 (TD_FAKE_GATEWAY=1): an offline gateway that walks the whole flow in Korean."""

from __future__ import annotations

import re

import pytest

from task_design.cli import DEFAULT_SEED_DIR
from task_design.costs import CostStore
from task_design.demo import FAIL_MARKER, build_demo_gateway, theme_of
from task_design.domain import DraftState, Level
from task_design.gateway import Role
from task_design.loader import load_dataset
from task_design.repositories import TaskRepository
from task_design.service import TaskDesignService
from task_design.testing.workflow import (
    ensure_competencies,
    make_input,
    make_task,
    seed_similar_tasks,
)

HANGUL = re.compile(r"[가-힣]")


def _service(db, settings):
    gateway = build_demo_gateway(settings, usage_sink=CostStore(db), delay=0)
    seed_similar_tasks(db, gateway, settings)
    ensure_competencies(db)
    return TaskDesignService(db, gateway, settings)


def test_demo_offers_three_distinct_korean_candidates(db, settings):
    service = _service(db, settings)

    draft = service.start(make_input(), author_id=1)

    assert draft.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert len({c.scenario for c in draft.candidates}) == 3
    for c in draft.candidates:
        assert HANGUL.search(c.title) and HANGUL.search(c.scenario)
        assert c.referenced_task_ids
    again = service.regenerate_scenarios(draft.id)
    assert not {c.scenario for c in again.candidates} & {c.scenario for c in draft.candidates}


def test_demo_draft_passes_verification_on_the_first_round(db, settings):
    service = _service(db, settings)
    draft = service.start(make_input(competency_ids=["C1", "C2"]), author_id=1)

    done = service.select_scenario(draft.id, draft.candidates[0].id)

    assert done.state is DraftState.AWAITING_REVIEW
    assert len(done.rounds) == 1 and done.rounds[0].verdict.passed
    assert done.failure_report is None
    task = done.task
    assert HANGUL.search(task.instruction)
    assert any(a.traps for a in task.attachments)
    assert task.rubric.total_points == 100
    means = {r.persona_level: r.mean_score for r in done.rounds[0].mock_exam_results}
    assert means[Level.HIGH] > means[Level.MEDIUM] > means[Level.LOW]
    assert CostStore(db).for_draft(draft.id)  # costs reach the dashboard


def test_demo_hard_draft_shows_one_self_correction_round(db, settings):
    service = _service(db, settings)
    draft = service.start(make_input(difficulty=Level.HIGH), author_id=1)

    done = service.select_scenario(draft.id, draft.candidates[1].id)

    assert [r.verdict.passed for r in done.rounds] == [False, True]
    assert done.failure_report is None


def test_demo_fail_marker_in_the_memo_ends_with_a_failure_report(db, settings):
    service = _service(db, settings)
    draft = service.start(make_input(memo=f"{FAIL_MARKER} 해 보기"), author_id=1)

    done = service.select_scenario(draft.id, draft.candidates[0].id)

    assert done.state is DraftState.AWAITING_REVIEW
    assert len(done.rounds) == settings.max_fix_rounds + 1
    assert done.failure_report is not None


def test_demo_attachments_and_submissions_fit_the_chosen_scenario(db, settings):
    service = _service(db, settings)
    draft = service.start(make_input(), author_id=1)
    chosen = draft.candidates[2]
    theme = theme_of(chosen.scenario)

    done = service.select_scenario(draft.id, chosen.id)

    assert theme.csv_name in done.task.instruction
    assert [a.filename for a in done.task.attachments] == [theme.csv_name, theme.memo_name]
    high = next(r for r in done.rounds[0].mock_exam_results if r.persona_level is Level.HIGH)
    assert theme.deliverable in high.output
    assert theme.header in high.transcript[1].content  # the AI 도구 read this theme's CSV


# --- 데모 임베딩: shared wording → higher cosine ----------------------------------------


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_demo_embeddings_give_higher_cosine_to_texts_that_share_wording(settings):
    a = "물류센터 냉동창고 온도기록에서 이상치를 찾는 상황"
    b = "냉동창고 온도기록의 이상치를 점검하는 업무"
    c = "신규 입사자를 위한 온보딩 체크리스트를 만드는 상황"
    [va, vb, vc] = build_demo_gateway(settings, delay=0).embed([a, b, c])

    assert _cos(va, va) == pytest.approx(1.0)
    assert _cos(va, vb) > 0.4
    assert _cos(va, vc) < 0.25
    assert _cos(va, vb) > _cos(va, vc) + 0.2
    assert build_demo_gateway(settings, delay=0).embed([a]) == [va]  # deterministic


def test_demo_candidates_over_the_seed_dataset_are_not_rejected_as_duplicates(
    empty_repositories, settings
):
    db = empty_repositories
    gateway = build_demo_gateway(settings, usage_sink=CostStore(db), delay=0)
    load_dataset(DEFAULT_SEED_DIR, db, gateway, settings)
    service = TaskDesignService(db, gateway, settings)

    draft = service.start(make_input(), author_id=1)

    assert draft.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert len(draft.candidates) == 3
    generations = [c for c in gateway.calls_for(Role.GENERATION) if c.draft_id == draft.id]
    assert len(generations) == 1  # no candidate was dropped as a duplicate


def test_stale_task_embeddings_are_refreshed_with_the_demo_embedding(
    empty_repositories, fake_gateway, settings
):
    db = empty_repositories
    old = TaskRepository(db, fake_gateway, settings)  # hash-based vectors
    ids = [old.add(make_task(f"분기 매출 보고서 검증 상황 {i}")) for i in range(3)]
    demo = TaskRepository(db, build_demo_gateway(settings, delay=0), settings)

    assert demo.refresh_stale_embeddings() == 3
    assert demo.refresh_stale_embeddings() == 0  # cheap no-op once current
    for task_id in ids:
        scenario = demo.get(task_id).scenario
        assert task_id in [m.task.id for m in demo.find_duplicates(scenario, threshold=0.999)]
