"""Workflow part 2 through the service: select_scenario → generation → mock exam → verdict."""

from __future__ import annotations

import threading
import uuid

import pytest

from task_design.domain import (
    Component,
    DraftState,
    FailureType,
    Level,
    PersonaStatus,
    Rubric,
    RubricItemType,
    Stage,
)
from task_design.gateway import Role
from task_design.service import TaskDesignService, UnknownCompetency
from task_design.testing.workflow import (
    Personas,
    install_verification,
    make_input,
    start_awaiting_review,
    start_awaiting_selection,
)
from task_design.workflow import UnknownCandidate


def test_start_rejects_competencies_not_in_the_framework(service, fake_gateway):
    with pytest.raises(UnknownCompetency):
        service.start(make_input(competency_ids=[f"NO-{uuid.uuid4().hex[:6]}"]), author_id=1)
    assert fake_gateway.calls == []


def test_select_rejects_a_candidate_not_currently_on_offer(service, fake_gateway, db, settings):
    draft = start_awaiting_selection(service, fake_gateway, db, settings)
    old = draft.candidates[0].id
    service.regenerate_scenarios(draft.id)

    for stale_or_unknown in (old, uuid.uuid4()):
        with pytest.raises(UnknownCandidate):
            service.select_scenario(draft.id, stale_or_unknown)

    after = service.get_draft(draft.id)
    assert after.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert after.selected_candidate_id is None


def test_selecting_a_candidate_builds_the_task_and_verifies_it_in_round_one(
    service, fake_gateway, db, settings
):
    scores = {Level.HIGH: 85, Level.MEDIUM: 60, Level.LOW: 30}

    draft = start_awaiting_review(service, fake_gateway, db, settings, scores=scores,
                                  candidate_index=1)

    assert draft.state is DraftState.AWAITING_REVIEW
    assert draft.stage is None
    assert draft.error is None
    chosen = draft.candidates[1]
    assert draft.selected_candidate_id == chosen.id
    task = draft.task
    assert task.scenario == chosen.scenario
    assert task.instruction
    assert task.attachments and all(a.traps for a in task.attachments)
    assert 3 <= len(task.rubric.items) <= 5
    assert any(i.type is RubricItemType.PROCESS for i in task.rubric.items)
    assert task.competency_ids == draft.input.competency_ids
    assert task.audience == draft.input.audience
    assert task.difficulty is draft.input.difficulty

    [round1] = draft.rounds
    assert round1.number == 1
    assert set(round1.changed_components) >= {
        Component.INSTRUCTION, Component.ATTACHMENTS, Component.RUBRIC}
    assert Component.SCENARIO not in round1.changed_components
    assert round1.change_reason
    assert round1.verdict.passed
    assert sorted(r.persona_level for r in round1.mock_exam_results) == sorted(Level)
    for result in round1.mock_exam_results:
        assert result.transcript and result.output
        assert len(result.gradings) == settings.gradings_per_submission == 3
        for grading in result.gradings:
            assert len(grading.item_scores) == len(task.rubric.items)
            assert all(s.rationale for s in grading.item_scores)
        assert result.mean_score == pytest.approx(scores[result.persona_level])
    assert draft.persona_status == {lv: PersonaStatus.DONE for lv in Level}


def test_every_model_call_is_charged_to_the_draft(service, fake_gateway, db, settings):
    draft = start_awaiting_review(service, fake_gateway, db, settings)

    roles = {Role.GENERATION, Role.REVIEW, Role.EXAMINEE, Role.AI_TOOL, Role.GRADING}
    calls = [c for c in fake_gateway.calls if c.role in roles]
    assert {c.role for c in calls} == roles
    assert {c.draft_id for c in calls} == {draft.id}


def test_three_personas_sit_the_mock_exam_concurrently_and_get_draft_shows_them(
    service, fake_gateway, db, settings
):
    all_three_asking = threading.Barrier(3, timeout=10)
    seen: dict[Level, dict] = {}

    def observe(level, call):
        if not any(m.role == "assistant" for m in call.messages):  # first move
            all_three_asking.wait()  # breaks unless all three personas run at once
            draft = service.get_draft(call.draft_id)
            seen[level] = {"stage": draft.stage, "status": dict(draft.persona_status),
                           "state": draft.state}

    draft = start_awaiting_review(service, fake_gateway, db, settings,
                                  personas=Personas(before_call=observe))

    assert draft.state is DraftState.AWAITING_REVIEW
    assert set(seen) == set(Level)
    for observed in seen.values():
        assert observed["state"] is DraftState.VERIFYING
        assert observed["stage"] is Stage.MOCK_EXAM
        assert observed["status"] == {lv: PersonaStatus.RUNNING for lv in Level}


H, M, L = Level.HIGH, Level.MEDIUM, Level.LOW


@pytest.mark.parametrize(
    ("scores", "failures"),
    [
        pytest.param({H: 85, M: 60, L: 30}, [], id="passes"),
        pytest.param({H: 70, M: 55, L: 40}, [], id="exactly-at-thresholds"),
        pytest.param({H: 65, M: 50, L: 20}, [FailureType.UNSOLVABLE], id="high-below-70"),
        pytest.param({H: 85, M: 75, L: 60}, [FailureType.LOW_DISCRIMINATION], id="gap-under-30"),
        pytest.param({H: 85, M: 30, L: 40}, [FailureType.LOW_DISCRIMINATION],
                     id="medium-below-low"),
        pytest.param({H: 85, M: 85, L: 30}, [FailureType.LOW_DISCRIMINATION],
                     id="high-not-above-medium"),
        pytest.param({H: [80, 85, 95], M: 60, L: 30}, [FailureType.INCONSISTENT_GRADING],
                     id="high-graded-inconsistently"),
        pytest.param({H: 85, M: 60, L: [20, 30, 40]}, [FailureType.INCONSISTENT_GRADING],
                     id="low-graded-inconsistently"),
        pytest.param({H: [50, 60, 70], M: 55, L: 50},
                     [FailureType.UNSOLVABLE, FailureType.LOW_DISCRIMINATION,
                      FailureType.INCONSISTENT_GRADING], id="everything-fails"),
    ],
)
def test_verdict_reports_each_failure_type_and_the_draft_still_goes_to_review(
    service, fake_gateway, db, settings, scores, failures
):
    draft = start_awaiting_review(service, fake_gateway, db, settings, scores=scores)

    assert draft.state is DraftState.AWAITING_REVIEW
    [round1] = draft.rounds
    assert round1.verdict.failures == failures
    assert round1.verdict.passed is (failures == [])


def test_verdict_thresholds_come_from_settings(fake_gateway, db, settings):
    strict = settings.model_copy(update={"solvable_min_score": 90})
    service = TaskDesignService(db, fake_gateway, strict)

    draft = start_awaiting_review(service, fake_gateway, db, strict,
                                  scores={H: 85, M: 60, L: 30})

    assert draft.rounds[0].verdict.failures == [FailureType.UNSOLVABLE]


def test_review_agent_finding_regenerates_the_rubric_once_before_the_mock_exam(
    service, fake_gateway, db, settings
):
    script = install_verification(fake_gateway)
    fake_gateway.script(Role.REVIEW, {"consistent": False, "target": "rubric",
                                      "issues": ["지시문이 요구한 요약 보고서를 채점하지 않음"]})

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    assert len(script.task_writer.rubrics) == 2
    assert draft.task.rubric == Rubric.model_validate(script.task_writer.rubrics[1])
    assert draft.task.instruction == script.task_writer.instructions[0]
    assert len(fake_gateway.calls_for(Role.REVIEW)) == 1
    assert draft.state is DraftState.AWAITING_REVIEW
    assert len(draft.rounds) == 1


def test_review_agent_finding_regenerates_the_instruction_once_before_the_mock_exam(
    service, fake_gateway, db, settings
):
    script = install_verification(fake_gateway)
    fake_gateway.script(Role.REVIEW, {"consistent": False, "target": "instruction",
                                      "issues": ["제출물 형식이 없음"]})

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    assert len(script.task_writer.instructions) == 2
    assert draft.task.instruction == script.task_writer.instructions[1]
    assert draft.task.rubric == Rubric.model_validate(script.task_writer.rubrics[0])
    assert len(fake_gateway.calls_for(Role.REVIEW)) == 1
    instruction_seen_by_personas = {
        c.prompt.count(script.task_writer.instructions[1]) > 0
        for c in fake_gateway.calls_for(Role.EXAMINEE)
    }
    assert instruction_seen_by_personas == {True}


def test_a_persona_gets_at_most_six_messages_to_the_ai_tool(service, fake_gateway, db, settings):
    endless = Personas(asks={Level.HIGH: 100})

    draft = start_awaiting_review(service, fake_gateway, db, settings, personas=endless)

    high = next(r for r in draft.rounds[0].mock_exam_results if r.persona_level is Level.HIGH)
    asked = [t for t in high.transcript if t.speaker == "examinee"]
    assert len(asked) == settings.max_examinee_turns == 6
    assert high.output
    assert len(high.gradings) == 3
