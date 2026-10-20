"""Human-time heartbeat: what the browser reports becomes 사람 활성 구간 of the right draft."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from task_design.domain import ReviewDecision
from task_design.testing.workflow import (
    install_verification,
    start_awaiting_review,
    start_awaiting_selection,
)
from task_design.ui.heartbeat import HeartbeatLedger


def _iso(t: datetime) -> str:
    return t.isoformat().replace("+00:00", "Z")


def _beat(seq, ctx, start, end):
    return {"seq": seq, "context": ctx, "start": _iso(start), "end": _iso(end)}


def _human_minutes(service, draft_id, reviewer=2):
    """Review the draft and read back its human minutes from the dashboard metrics."""
    labels = {"scenario": "keep", "instruction": "keep", "rubric": "keep"}
    service.review(draft_id, reviewer, labels, ReviewDecision.CONFIRM)
    now = datetime.now(UTC)
    m = service.metrics({"start": now - timedelta(minutes=5), "end": now + timedelta(seconds=1)})
    return next(w.human_minutes for w in m.work_times if w.draft_id == draft_id)


def test_beats_for_a_draft_count_as_its_human_time(service, fake_gateway, db, settings):
    draft = start_awaiting_review(service, fake_gateway, db, settings, author_id=1)
    ledger = HeartbeatLedger()
    t0 = datetime.now(UTC) - timedelta(minutes=10)

    acked = ledger.receive(service, staff_id=1, payload={"beats": [
        _beat(1, str(draft.id), t0, t0 + timedelta(minutes=2)),
        _beat(2, str(draft.id), t0 + timedelta(minutes=2), t0 + timedelta(minutes=3)),
    ]})

    assert acked == 2
    assert _human_minutes(service, draft.id) == 3.0


def test_resent_beats_are_not_counted_twice(service, fake_gateway, db, settings):
    draft = start_awaiting_review(service, fake_gateway, db, settings, author_id=1)
    ledger = HeartbeatLedger()
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    beat = _beat(1, str(draft.id), t0, t0 + timedelta(minutes=2))

    ledger.receive(service, staff_id=1, payload={"beats": [beat]})
    ledger.receive(service, staff_id=1, payload={"beats": [beat]})

    assert _human_minutes(service, draft.id) == 2.0


def test_time_on_the_input_form_is_credited_to_the_draft_it_creates(
    service, fake_gateway, db, settings
):
    ledger = HeartbeatLedger()
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    ledger.receive(service, staff_id=1, payload={"beats": [
        _beat(1, "", t0, t0 + timedelta(minutes=4)),  # no draft yet: 입력 폼
    ]})
    install_verification(fake_gateway)
    draft = start_awaiting_selection(service, fake_gateway, db, settings, author_id=1)

    ledger.credit_pending(service, draft.id, staff_id=1)

    assert ledger.pending == []
    service.select_scenario(draft.id, draft.candidates[0].id)
    assert _human_minutes(service, draft.id) == 4.0


def test_bad_beats_are_dropped_without_breaking_the_page(service):
    ledger = HeartbeatLedger()

    acked = ledger.receive(service, staff_id=1, payload={"beats": [
        {"seq": 7, "context": "not-a-uuid", "start": "x", "end": "y"},
    ]})

    assert acked == 7


def test_form_time_reported_after_the_draft_was_created_still_goes_to_that_draft(
    service, fake_gateway, db, settings
):
    # The browser closes the 입력 폼 segment only when the screen switches to the new
    # draft, i.e. after 시작 already credited what was pending.
    ledger = HeartbeatLedger()
    install_verification(fake_gateway)
    form_opened = datetime.now(UTC) - timedelta(minutes=3)
    draft = start_awaiting_selection(service, fake_gateway, db, settings, author_id=1)
    ledger.credit_pending(service, draft.id, staff_id=1)

    ledger.receive(service, staff_id=1, payload={"beats": [
        _beat(1, "", form_opened, datetime.now(UTC)),
    ]})

    assert ledger.pending == []
    service.select_scenario(draft.id, draft.candidates[0].id)
    assert _human_minutes(service, draft.id) >= 2.9
