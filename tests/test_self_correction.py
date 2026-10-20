"""자기 수정 루프 through the service: verdict failure → fix one component → re-verify."""

from __future__ import annotations

import pytest

from task_design.costs import CostStore
from task_design.domain import Component, DraftState, FailureType, Level, Rubric
from task_design.gateway import Role
from task_design.service import TaskDesignService
from task_design.testing.fake_gateway import FakeGateway, FakeResponse
from task_design.testing.workflow import (
    Grader,
    TaskWriter,
    install_verification,
    start_awaiting_review,
)

H, M, L = Level.HIGH, Level.MEDIUM, Level.LOW


def test_a_draft_that_fails_round_one_and_passes_round_two_goes_to_review_without_a_report(
    service, fake_gateway, db, settings
):
    scores = {H: [60, 60, 60, 85, 85, 85], M: 50, L: 20}  # round 1 unsolvable, round 2 passes

    draft = start_awaiting_review(service, fake_gateway, db, settings, scores=scores)

    assert draft.state is DraftState.AWAITING_REVIEW
    assert [r.number for r in draft.rounds] == [1, 2]
    assert not draft.rounds[0].verdict.passed
    assert draft.rounds[1].verdict.passed
    assert draft.failure_report is None


def _passing_from_round(n: int, round1: dict) -> dict:
    """Scores that are ``round1`` for rounds 1..n-1 and pass from round n."""
    passing = {H: 85, M: 60, L: 30}
    return {lv: [round1[lv]] * 3 * (n - 1) + [passing[lv]] * 3 for lv in Level}


def test_unsolvable_rewrites_only_the_instruction_and_keeps_the_scenario(
    service, fake_gateway, db, settings
):
    script = install_verification(fake_gateway, scores=_passing_from_round(2, {H: 60, M: 50, L: 20}))

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    assert draft.rounds[0].verdict.failures == [FailureType.UNSOLVABLE]
    round2 = draft.rounds[1]
    assert round2.changed_components == [Component.INSTRUCTION]
    assert round2.change_reason
    assert draft.task.scenario == draft.candidates[0].scenario
    assert draft.task.instruction == script.task_writer.instructions[1]
    assert draft.task.rubric == Rubric.model_validate(script.task_writer.rubrics[0])
    assert draft.task.attachments == script.task_writer.attachment_models(0)


def test_weak_discrimination_rewrites_the_rubric_then_the_traps_and_resyncs_the_criteria(
    service, fake_gateway, db, settings
):
    script = install_verification(fake_gateway,
                                  scores=_passing_from_round(3, {H: 85, M: 75, L: 60}))

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    writer = script.task_writer
    assert [r.verdict.failures for r in draft.rounds] == [
        [FailureType.LOW_DISCRIMINATION], [FailureType.LOW_DISCRIMINATION], []]
    assert [r.changed_components for r in draft.rounds[1:]] == [
        [Component.RUBRIC], [Component.ATTACHMENTS, Component.RUBRIC]]
    assert draft.task.scenario == draft.candidates[0].scenario
    assert draft.task.instruction == writer.instructions[0]
    # 함정 fix: only the traps change; the files and their formats stay
    original, retrapped = writer.attachment_models(0), writer.attachment_models(1)
    assert [(a.filename, a.format) for a in draft.task.attachments] == [
        (a.filename, a.format) for a in original]
    assert [a.traps for a in draft.task.attachments] == [a.traps for a in retrapped]
    # ...and the rubric's criteria wording follows the new traps; items, types, points stay
    structure = Rubric.model_validate(writer.rubrics[1])
    reworded = Rubric.model_validate(writer.rubrics[2])
    for item, old, new in zip(draft.task.rubric.items, structure.items, reworded.items,
                              strict=True):
        assert (item.title, item.type, item.points, item.competency_id) == (
            old.title, old.type, old.points, old.competency_id)
        assert (item.criteria_zero, item.criteria_partial, item.criteria_full) == (
            new.criteria_zero, new.criteria_partial, new.criteria_full)


def test_inconsistent_grading_rewrites_only_the_rubric_criteria_wording(
    service, fake_gateway, db, settings
):
    script = install_verification(
        fake_gateway, scores=_passing_from_round(2, {H: 85, M: 60, L: 30})
        | {H: [80, 85, 95, 85, 85, 85]})

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    assert draft.rounds[0].verdict.failures == [FailureType.INCONSISTENT_GRADING]
    assert draft.rounds[1].changed_components == [Component.RUBRIC]
    before = Rubric.model_validate(script.task_writer.rubrics[0])
    rewritten = Rubric.model_validate(script.task_writer.rubrics[1])
    after = draft.task.rubric
    for old, new, written in zip(before.items, after.items, rewritten.items, strict=True):
        assert (new.title, new.type, new.points, new.competency_id) == (
            old.title, old.type, old.points, old.competency_id)
        assert (new.criteria_zero, new.criteria_partial, new.criteria_full) == (
            written.criteria_zero, written.criteria_partial, written.criteria_full)
    assert draft.task.instruction == script.task_writer.instructions[0]
    assert draft.task.scenario == draft.candidates[0].scenario


def test_several_failures_are_fixed_one_at_a_time_in_order(service, fake_gateway, db, settings):
    script = install_verification(fake_gateway, scores={
        H: [50, 60, 70] + [65, 75, 85] + [85] * 3, M: 55, L: 50})  # 3 fail → 2 fail → pass

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    assert draft.rounds[0].verdict.failures[0] is FailureType.UNSOLVABLE
    assert draft.rounds[1].changed_components == [Component.INSTRUCTION]
    assert draft.rounds[1].verdict.failures == [
        FailureType.LOW_DISCRIMINATION, FailureType.INCONSISTENT_GRADING]
    assert draft.rounds[2].changed_components == [Component.RUBRIC]
    # a whole new rubric (변별력 fix), not just reworded criteria (채점 불일치 fix)
    assert draft.task.rubric == Rubric.model_validate(script.task_writer.rubrics[1])
    assert draft.rounds[2].verdict.passed


def test_each_round_keeps_its_own_mock_exam_results_on_the_updated_task(
    service, fake_gateway, db, settings
):
    script = install_verification(fake_gateway, scores=_passing_from_round(2, {H: 60, M: 50, L: 20}))

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    round1, round2 = draft.rounds
    assert [r.mean_score for r in round1.mock_exam_results] == [60, 50, 20]
    assert [r.mean_score for r in round2.mock_exam_results] == [85, 60, 30]
    new_instruction = script.task_writer.instructions[1]
    persona_calls = fake_gateway.calls_for(Role.EXAMINEE)
    assert any(new_instruction in c.prompt for c in persona_calls)
    assert len(round2.mock_exam_results) == 3
    assert all(len(r.gradings) == 3 for r in round2.mock_exam_results)


def test_a_draft_still_failing_after_three_fixes_goes_to_review_with_a_failure_report(
    service, fake_gateway, db, settings
):
    scores = {H: 60, M: 50, L: 20}  # unsolvable every round

    draft = start_awaiting_review(service, fake_gateway, db, settings, scores=scores)

    assert draft.state is DraftState.AWAITING_REVIEW
    assert settings.max_fix_rounds == 3
    assert [r.number for r in draft.rounds] == [1, 2, 3, 4]  # round 1 + 3 fixes
    report = draft.failure_report
    assert report is not None
    assert report.failure_types == [FailureType.UNSOLVABLE]
    assert report.scores_by_round == [{H: 60, M: 50, L: 20}] * 4
    assert len(report.attempted_fixes) == 3
    assert report.stopped_by_token_budget is False
    assert all(r.changed_components == [Component.INSTRUCTION] for r in draft.rounds[1:])
    assert draft.task.scenario == draft.candidates[0].scenario


def test_the_number_of_fixes_comes_from_settings(fake_gateway, db, settings):
    one_fix = settings.model_copy(update={"max_fix_rounds": 1})
    service = TaskDesignService(db, fake_gateway, one_fix)

    draft = start_awaiting_review(service, fake_gateway, db, one_fix, scores={H: 60, M: 50, L: 20})

    assert len(draft.rounds) == 2
    assert len(draft.failure_report.attempted_fixes) == 1


EXPENSIVE = 10**9  # output tokens of one scripted call: blows any test budget
BUDGET = 10**8  # far above what a normal fake draft uses


@pytest.fixture
def metered(db, settings):
    """Factory: (service, gateway, settings) whose fake gateway records usage in the
    cost table, so the token budget sees it."""

    def make(**update):
        changed = settings.model_copy(update=update)
        gateway = FakeGateway(embedding_dim=changed.embedding_dim, usage_sink=CostStore(db))
        return TaskDesignService(db, gateway, changed), gateway, changed

    return make


def test_a_draft_over_its_token_budget_before_the_mock_exam_goes_to_review_unverified(
    db, metered
):
    service, gateway, tight = metered(draft_token_budget=1)

    draft = start_awaiting_review(service, gateway, db, tight, scores={H: 60, M: 50, L: 20})

    assert draft.state is DraftState.AWAITING_REVIEW
    assert draft.rounds == []
    assert gateway.calls_for(Role.EXAMINEE) == []
    assert draft.task is not None
    report = draft.failure_report
    assert report.stopped_by_token_budget is True
    assert report.scores_by_round == []


def test_the_loop_does_not_fix_once_the_verdict_leaves_the_draft_over_budget(db, metered):
    service, gateway, budgeted = metered(draft_token_budget=BUDGET)
    grader = Grader({H: 60, M: 50, L: 20})
    gateway.respond_with(Role.GRADING,
                         lambda call: FakeResponse(grader(call), output_tokens=EXPENSIVE))

    draft = start_awaiting_review(service, gateway, db, budgeted)

    assert len(draft.rounds) == 1
    report = draft.failure_report
    assert report.stopped_by_token_budget is True
    assert report.failure_types == [FailureType.UNSOLVABLE]
    assert report.scores_by_round == [{H: 60, M: 50, L: 20}]
    assert report.attempted_fixes == []


def test_a_fix_that_exhausts_the_budget_is_not_mock_examined_again(db, metered):
    service, gateway, budgeted = metered(draft_token_budget=BUDGET)
    writer = TaskWriter(output_tokens=lambda part, n: EXPENSIVE if (part, n) == (
        "instruction", 2) else None)
    install_verification(gateway, scores={H: 60, M: 50, L: 20}, task_writer=writer)

    draft = start_awaiting_review(service, gateway, db, budgeted)

    assert [r.number for r in draft.rounds] == [1]
    assert draft.task.instruction == writer.instructions[1]  # the fix is kept, unverified
    report = draft.failure_report
    assert report.stopped_by_token_budget is True
    assert report.failure_types == [FailureType.UNSOLVABLE]
    assert len(report.attempted_fixes) == 1


def test_a_draft_within_its_token_budget_keeps_correcting(db, metered):
    service, gateway, settings = metered()

    draft = start_awaiting_review(service, gateway, db, settings,
                                  scores=_passing_from_round(2, {H: 60, M: 50, L: 20}))

    assert len(draft.rounds) == 2
    assert draft.failure_report is None
