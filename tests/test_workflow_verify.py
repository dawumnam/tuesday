"""Workflow part 2 through the service: select_scenario → generation → mock exam → verdict."""

from __future__ import annotations

import uuid

import pytest

from task_design.domain import DraftState
from task_design.service import UnknownCompetency
from task_design.testing.workflow import make_input, start_awaiting_selection
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
