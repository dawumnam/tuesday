"""Runs cut off by an app restart (the background job died with the process).

On start-up the app calls ``recover_interrupted_runs``: drafts left in 생성 중 / 검증 중
without an error get one, so the 재시도 button can pick them up again.
"""

from __future__ import annotations

from task_design.domain import DraftState
from task_design.gateway import Role
from task_design.service import TaskDesignService
from task_design.testing.workflow import (
    ScenarioWriter,
    by_schema,
    install_verification,
    make_input,
    seed_similar_tasks,
    start_awaiting_selection,
)
from task_design.workflow.schemas import CandidateBatch


def _dropping_service(db, gateway, settings):
    """A service whose background jobs never run (the process died before they did)."""
    return TaskDesignService(db, gateway, settings, run_graph=lambda job: None)


def test_a_start_cut_off_by_a_restart_can_be_retried(db, fake_gateway, settings, service):
    seed_similar_tasks(db, fake_gateway, settings)
    fake_gateway.respond_with(Role.GENERATION, by_schema({CandidateBatch: ScenarioWriter()}))
    stuck = _dropping_service(db, fake_gateway, settings).start(make_input(), author_id=1)
    assert stuck.state is DraftState.GENERATING and stuck.error is None

    recovered = service.recover_interrupted_runs()

    assert stuck.id in recovered
    assert service.get_draft(stuck.id).error
    retried = service.retry(stuck.id)
    assert retried.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert len(retried.candidates) == 3


def test_a_selection_cut_off_by_a_restart_can_be_retried(db, fake_gateway, settings, service):
    install_verification(fake_gateway)
    draft = start_awaiting_selection(service, fake_gateway, db, settings)
    _dropping_service(db, fake_gateway, settings).select_scenario(
        draft.id, draft.candidates[0].id
    )
    assert service.get_draft(draft.id).state is DraftState.VERIFYING

    service.recover_interrupted_runs()
    retried = service.retry(draft.id)

    assert retried.error is None
    assert retried.state is DraftState.AWAITING_REVIEW
    assert retried.selected_candidate_id == draft.candidates[0].id


def test_drafts_waiting_on_a_person_are_left_alone(db, fake_gateway, settings, service):
    draft = start_awaiting_selection(service, fake_gateway, db, settings)

    assert draft.id not in service.recover_interrupted_runs()
    assert service.get_draft(draft.id).error is None
