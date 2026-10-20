"""측정 through the service: record_activity → metrics(기간)."""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from task_design.costs import CostStore
from task_design.domain import Component, DraftState, Interval, ReviewDecision, ReviewLabel
from task_design.service import (
    DraftNotFound,
    InvalidActivityInterval,
    InvalidPeriod,
    TaskDesignService,
    UnknownStaff,
)
from task_design.settings import ModelPrice
from task_design.testing.fake_gateway import FakeGateway
from task_design.testing.workflow import (
    PASSING_SCORES,
    install_verification,
    make_input,
    seed_similar_tasks,
    start_awaiting_review,
    start_awaiting_selection,
)

AUTHOR, REVIEWER = 1, 2

ALL_KEEP = {
    Component.SCENARIO: ReviewLabel.KEEP,
    Component.INSTRUCTION: ReviewLabel.KEEP,
    Component.RUBRIC: ReviewLabel.KEEP,
}


def now() -> datetime:
    return datetime.now(UTC)


def bracket(start: datetime) -> Interval:
    """A period from ``start`` to just after now: holds only what this test did."""
    return Interval(start=start, end=now() + timedelta(seconds=1))


def reviewed(service, fake, db, settings, labels, decision):
    draft = start_awaiting_review(service, fake, db, settings, author_id=AUTHOR)
    return service.review(draft.id, REVIEWER, labels, decision)


def test_pass_ratio_counts_confirmed_drafts_without_a_full_rewrite_over_all_reviewed(
    service, fake_gateway, db, settings
):
    began = now()
    keep = reviewed(service, fake_gateway, db, settings, ALL_KEEP, ReviewDecision.CONFIRM)
    partial = reviewed(
        service, fake_gateway, db, settings,
        {**ALL_KEEP, Component.INSTRUCTION: ReviewLabel.PARTIAL_EDIT}, ReviewDecision.CONFIRM,
    )
    rewritten = reviewed(
        service, fake_gateway, db, settings,
        {**ALL_KEEP, Component.RUBRIC: ReviewLabel.FULL_REWRITE}, ReviewDecision.CONFIRM,
    )
    rejected = reviewed(service, fake_gateway, db, settings, ALL_KEEP, ReviewDecision.REJECT)
    start_awaiting_review(service, fake_gateway, db, settings, author_id=AUTHOR)  # unreviewed

    m = service.metrics(bracket(began))

    assert m.reviewed_drafts == 4
    assert m.passed_drafts == 2  # keep + partial; full rewrite and 반려 fail
    assert m.pass_without_full_rewrite_ratio == pytest.approx(0.5)
    assert m.meets_pass_ratio_target is False  # 70% by default
    assert {d.draft_id for d in m.work_times} == {keep.id, partial.id, rewritten.id, rejected.id}
    assert m.label_distribution[Component.RUBRIC] == {
        ReviewLabel.KEEP: 3, ReviewLabel.PARTIAL_EDIT: 0, ReviewLabel.FULL_REWRITE: 1,
    }
    assert m.label_distribution[Component.INSTRUCTION][ReviewLabel.PARTIAL_EDIT] == 1


def test_a_period_without_reviews_has_no_ratio_or_time(service):
    m = service.metrics(Interval(start=datetime(2001, 1, 1, tzinfo=UTC),
                                 end=datetime(2001, 2, 1, tzinfo=UTC)))

    assert m.reviewed_drafts == 0
    assert m.pass_without_full_rewrite_ratio is None
    assert m.mean_total_work_minutes is None
    assert m.meets_pass_ratio_target is None


class SteppingClock:
    """Each reading is ``step`` later than the one before."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.now, self.step = start - step, step

    def __call__(self) -> datetime:
        self.now += self.step
        return self.now


def at(minute: float) -> datetime:
    return datetime(2030, 1, 1, 10, 0, tzinfo=UTC) + timedelta(minutes=minute)


def span(a: float, b: float) -> Interval:
    return Interval(start=at(a), end=at(b))


@pytest.fixture
def timed_service(db, fake_gateway, settings):
    # Agent runs read the clock at start and end: every run lasts exactly 1 minute.
    clock = SteppingClock(datetime(2031, 1, 1, tzinfo=UTC), timedelta(minutes=1))
    return TaskDesignService(db=db, gateway=fake_gateway, settings=settings, clock=clock)


def test_total_work_time_is_human_active_time_plus_agent_run_time_without_gaps_or_overlaps(
    timed_service, fake_gateway, db, settings
):
    service = timed_service
    began = now()
    draft = start_awaiting_review(service, fake_gateway, db, settings, author_id=AUTHOR)
    service.record_activity(draft.id, AUTHOR, span(0, 5))
    service.record_activity(draft.id, AUTHOR, span(3, 8))  # overlaps the first: 0–8
    service.record_activity(draft.id, REVIEWER, span(7, 9))  # another tab, overlapping: 0–9
    service.record_activity(draft.id, REVIEWER, span(30, 32))  # gap 9–30 is not counted
    service.review(draft.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)

    m = service.metrics(bracket(began))

    [work] = m.work_times
    assert work.human_minutes == pytest.approx(11)
    assert work.agent_minutes == pytest.approx(2)  # start, select_scenario: 1 minute each
    assert work.total_minutes == pytest.approx(13)
    assert m.mean_total_work_minutes == pytest.approx(13)
    assert m.meets_work_minutes_target is True


def test_every_graph_execution_counts_as_agent_time_also_when_run_in_the_background(
    db, fake_gateway, settings
):
    pool = ThreadPoolExecutor(max_workers=1)
    service = TaskDesignService(db=db, gateway=fake_gateway, settings=settings,
                                run_graph=pool.submit,
                                clock=SteppingClock(at(0), timedelta(minutes=1)))
    began = now()
    try:
        install_verification(fake_gateway, scores=PASSING_SCORES, competency_ids=["C1"])
        seed_similar_tasks(db, fake_gateway, settings)
        draft = service.start(make_input(), AUTHOR)
        draft = wait_until(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
        service.regenerate_scenarios(draft.id)
        draft = wait_until(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
        service.select_scenario(draft.id, draft.candidates[0].id)
        wait_until(service, draft.id, DraftState.AWAITING_REVIEW)
    finally:
        pool.shutdown(wait=True)
    service.review(draft.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)

    [work] = service.metrics(bracket(began)).work_times

    assert work.agent_minutes == pytest.approx(3)  # start, regenerate, select: 1 minute each
    assert work.human_minutes == 0


def test_costs_sum_every_model_call_of_drafts_created_in_the_period_with_a_trace_link(
    db, settings
):
    settings = settings.model_copy(update={"langfuse_host": "https://lf.example/",
                                           "langfuse_project_id": "proj-1"})
    # $1 per token, so a draft's cost equals its token count.
    fake = FakeGateway(embedding_dim=settings.embedding_dim, usage_sink=CostStore(db),
                       price=ModelPrice(input_usd_per_mtok=1e6, output_usd_per_mtok=1e6))
    service = TaskDesignService(db=db, gateway=fake, settings=settings)
    install_verification(fake, scores=PASSING_SCORES, competency_ids=["C1"])
    earlier = start_awaiting_selection(service, fake, db, settings, author_id=AUTHOR)
    began = now()
    reviewed_draft = start_awaiting_review(service, fake, db, settings, author_id=AUTHOR)
    service.review(reviewed_draft.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)
    unreviewed = start_awaiting_selection(service, fake, db, settings, author_id=AUTHOR)
    service.regenerate_scenarios(earlier.id)  # a later call of a draft created before

    m = service.metrics(bracket(began))

    by_id = {c.draft_id: c for c in m.draft_costs}
    assert set(by_id) == {reviewed_draft.id, unreviewed.id}
    for draft_id, cost in by_id.items():
        calls = [c for c in fake.calls if c.draft_id == draft_id]
        assert cost.calls == len(calls) > 0
        assert cost.input_tokens + cost.output_tokens == fake.total_tokens(draft_id)
        assert cost.estimated_cost_usd == pytest.approx(fake.total_tokens(draft_id))
        assert cost.trace_url == f"https://lf.example/project/proj-1/traces/{draft_id}"
    assert m.total_calls == sum(c.calls for c in by_id.values())
    assert m.total_cost_usd == pytest.approx(
        fake.total_tokens(reviewed_draft.id) + fake.total_tokens(unreviewed.id))
    roles = {c.role for c in m.costs if c.draft_id == reviewed_draft.id}
    assert {"generation", "examinee", "grading"} <= roles
    assert m.work_times[0].trace_url == by_id[reviewed_draft.id].trace_url


def test_without_a_langfuse_project_there_are_no_trace_links(service, fake_gateway, db, settings):
    began = now()
    start_awaiting_selection(service, fake_gateway, db, settings, author_id=AUTHOR)

    [cost] = service.metrics(bracket(began)).draft_costs

    assert cost.trace_url is None


def test_activity_must_name_an_existing_draft_and_staff_and_a_well_formed_interval(
    service, fake_gateway, db, settings
):
    draft = start_awaiting_selection(service, fake_gateway, db, settings, author_id=AUTHOR)

    with pytest.raises(DraftNotFound):
        service.record_activity(uuid.uuid4(), AUTHOR, span(0, 1))
    with pytest.raises(UnknownStaff):
        service.record_activity(draft.id, 999, span(0, 1))
    with pytest.raises(InvalidActivityInterval):
        service.record_activity(draft.id, AUTHOR, {"start": at(5), "end": at(1)})
    with pytest.raises(InvalidActivityInterval):
        service.record_activity(
            draft.id, AUTHOR, {"start": "2030-01-01T10:00:00", "end": "2030-01-01T10:05:00"})
    with pytest.raises(InvalidPeriod):
        service.metrics({"start": at(5)})

    # dicts with ISO strings (what the UI's heartbeat sends) are accepted
    service.record_activity(
        draft.id, AUTHOR, {"start": "2030-01-01T10:00:00Z", "end": "2030-01-01T10:05:00Z"})


def wait_until(service, draft_id, state, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    while (draft := service.get_draft(draft_id)).state is not state:
        assert draft.error is None, draft.error
        assert time.monotonic() < deadline, f"draft stuck in {draft.state}"
        time.sleep(0.02)
    return draft


def test_mean_work_time_is_compared_with_configurable_targets(db, fake_gateway, settings):
    settings = settings.model_copy(update={"work_minutes_target": 10.0,
                                           "pass_ratio_target": 0.5})
    service = TaskDesignService(db=db, gateway=fake_gateway, settings=settings,
                                clock=SteppingClock(at(0), timedelta(minutes=1)))
    began = now()
    a = start_awaiting_review(service, fake_gateway, db, settings, author_id=AUTHOR)
    b = start_awaiting_review(service, fake_gateway, db, settings, author_id=AUTHOR)
    service.record_activity(a.id, AUTHOR, span(0, 10))
    service.record_activity(b.id, AUTHOR, span(0, 20))
    service.review(a.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)
    service.review(b.id, REVIEWER, ALL_KEEP, ReviewDecision.REJECT)

    m = service.metrics(bracket(began))

    assert sorted(w.total_minutes for w in m.work_times) == pytest.approx([12, 22])
    assert m.mean_total_work_minutes == pytest.approx(17)
    assert (m.work_minutes_target, m.meets_work_minutes_target) == (10.0, False)
    assert (m.pass_ratio_target, m.meets_pass_ratio_target) == (0.5, True)
