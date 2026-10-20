"""Listing drafts and competencies for the screens (초안 목록, 입력 폼)."""

from __future__ import annotations

from task_design.domain import DraftState
from task_design.testing.workflow import (
    ensure_competencies,
    start_awaiting_review,
    start_awaiting_selection,
)


def test_list_drafts_shows_a_staff_members_own_drafts_newest_first(
    service, fake_gateway, db, settings
):
    first = start_awaiting_selection(service, fake_gateway, db, settings, author_id=2)
    second = start_awaiting_selection(service, fake_gateway, db, settings, author_id=2)

    mine = service.list_drafts(author_id=2)

    ids = [d.id for d in mine]
    assert ids.index(second.id) < ids.index(first.id)
    assert all(d.author_id == 2 for d in mine)
    summary = next(d for d in mine if d.id == first.id)
    assert summary.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert summary.title  # something a person can recognise the draft by
    assert summary.has_error is False


def test_list_drafts_filters_by_state_for_the_review_queue(service, fake_gateway, db, settings):
    waiting = start_awaiting_review(service, fake_gateway, db, settings, author_id=1)
    selecting = start_awaiting_selection(service, fake_gateway, db, settings, author_id=1)

    queue = service.list_drafts(states=[DraftState.AWAITING_REVIEW])

    ids = {d.id for d in queue}
    assert waiting.id in ids
    assert selecting.id not in ids
    summary = next(d for d in queue if d.id == waiting.id)
    selected = next(c for c in waiting.candidates if c.id == waiting.selected_candidate_id)
    assert summary.title == selected.title


def test_list_competencies_gives_the_competency_framework(service, db):
    ensure_competencies(db)

    ids = [c.id for c in service.list_competencies()]

    assert {"C1", "C2"} <= set(ids)
    assert ids == sorted(ids)
