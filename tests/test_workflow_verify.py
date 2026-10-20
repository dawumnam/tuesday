"""Workflow part 2 through the service: select_scenario → generation → mock exam → verdict."""

from __future__ import annotations

import uuid

import pytest

from task_design.domain import Component, DraftState, Level, PersonaStatus, RubricItemType
from task_design.gateway import Role
from task_design.service import UnknownCompetency
from task_design.testing.workflow import (
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
