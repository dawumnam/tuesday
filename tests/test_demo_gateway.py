"""데모 모드 (TD_FAKE_GATEWAY=1): an offline gateway that walks the whole flow in Korean."""

from __future__ import annotations

import re

from task_design.costs import CostStore
from task_design.demo import FAIL_MARKER, build_demo_gateway, theme_of
from task_design.domain import DraftState, Level
from task_design.service import TaskDesignService
from task_design.testing.workflow import ensure_competencies, make_input, seed_similar_tasks

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
    assert len(done.rounds) == settings.max_rounds
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
