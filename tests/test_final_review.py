"""최종 검토 through the service: review → 확정 (into the 과제 DB) or 반려."""

from __future__ import annotations

import uuid

import pytest

from task_design.domain import Component, DraftState, ReviewDecision, ReviewLabel
from task_design.repositories import TaskRepository
from task_design.service import (
    InvalidDraftState,
    InvalidEditedTask,
    InvalidReviewLabels,
    SelfReview,
)
from task_design.testing.workflow import start_awaiting_review, start_awaiting_selection

AUTHOR, REVIEWER = 1, 2

ALL_KEEP = {
    Component.SCENARIO: ReviewLabel.KEEP,
    Component.INSTRUCTION: ReviewLabel.KEEP,
    Component.RUBRIC: ReviewLabel.KEEP,
}


@pytest.fixture
def awaiting_review(empty_repositories, service, fake_gateway, db, settings):
    # empty_repositories: search results hold only this test's tasks.
    return start_awaiting_review(service, fake_gateway, db, settings, author_id=AUTHOR)


def test_confirming_puts_the_edited_task_in_the_task_db_where_search_finds_it(
    service, awaiting_review, db, fake_gateway, settings
):
    draft = awaiting_review
    edited_scenario = f"검토자가 고친 상황: 분기 재고 보고서 오류 {uuid.uuid4().hex[:8]}"
    edited = draft.task.model_copy(update={"scenario": edited_scenario})
    labels = {**ALL_KEEP, Component.SCENARIO: ReviewLabel.PARTIAL_EDIT}

    reviewed = service.review(draft.id, REVIEWER, labels, ReviewDecision.CONFIRM, edited)

    assert reviewed.state is DraftState.CONFIRMED
    assert reviewed.review.reviewer_id == REVIEWER
    assert reviewed.review.labels == labels
    assert reviewed.review.decision is ReviewDecision.CONFIRM
    assert reviewed.review.reviewed_at is not None
    hits = TaskRepository(db, fake_gateway, settings).hybrid_search(edited_scenario, k=5)
    [found] = [t for t in hits if t.scenario == edited_scenario]
    assert found.instruction == draft.task.instruction
    assert found.rubric == draft.task.rubric


def test_an_author_cannot_review_their_own_draft(service, awaiting_review):
    with pytest.raises(SelfReview):
        service.review(awaiting_review.id, AUTHOR, ALL_KEEP, ReviewDecision.CONFIRM)

    after = service.get_draft(awaiting_review.id)
    assert after.state is DraftState.AWAITING_REVIEW
    assert after.review is None


@pytest.mark.parametrize("missing", [Component.SCENARIO, Component.INSTRUCTION, Component.RUBRIC])
def test_a_review_missing_a_label_is_rejected(service, awaiting_review, missing):
    labels = {c: v for c, v in ALL_KEEP.items() if c is not missing}

    for decision in ReviewDecision:
        with pytest.raises(InvalidReviewLabels):
            service.review(awaiting_review.id, REVIEWER, labels, decision)

    after = service.get_draft(awaiting_review.id)
    assert after.state is DraftState.AWAITING_REVIEW
    assert after.review is None


def test_rejecting_keeps_the_draft_with_its_labels_and_out_of_the_task_db(
    service, awaiting_review, db, fake_gateway, settings
):
    labels = {
        Component.SCENARIO: ReviewLabel.KEEP,
        Component.INSTRUCTION: ReviewLabel.PARTIAL_EDIT,
        Component.RUBRIC: ReviewLabel.FULL_REWRITE,
    }

    service.review(awaiting_review.id, REVIEWER, labels, ReviewDecision.REJECT)

    after = service.get_draft(awaiting_review.id)
    assert after.state is DraftState.REJECTED
    assert after.review.labels == labels
    assert after.review.decision is ReviewDecision.REJECT
    assert after.review.reviewer_id == REVIEWER
    assert after.task == awaiting_review.task
    hits = TaskRepository(db, fake_gateway, settings).hybrid_search(
        awaiting_review.task.scenario, k=5
    )
    assert awaiting_review.task.instruction not in [t.instruction for t in hits]


def test_confirming_without_edits_stores_the_agents_task(
    service, awaiting_review, db, fake_gateway, settings
):
    service.review(awaiting_review.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)

    hits = TaskRepository(db, fake_gateway, settings).hybrid_search(
        awaiting_review.task.scenario, k=5
    )
    [found] = [t for t in hits if t.instruction == awaiting_review.task.instruction]
    assert found.scenario == awaiting_review.task.scenario
    assert found.rubric == awaiting_review.task.rubric


def test_a_draft_is_reviewed_only_once(service, awaiting_review):
    service.review(awaiting_review.id, REVIEWER, ALL_KEEP, ReviewDecision.REJECT)

    with pytest.raises(InvalidDraftState):
        service.review(awaiting_review.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)

    assert service.get_draft(awaiting_review.id).state is DraftState.REJECTED


def test_a_draft_not_yet_awaiting_review_cannot_be_reviewed(service, fake_gateway, db, settings):
    draft = start_awaiting_selection(service, fake_gateway, db, settings, author_id=AUTHOR)

    with pytest.raises(InvalidDraftState):
        service.review(draft.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)

    assert service.get_draft(draft.id).state is DraftState.AWAITING_SCENARIO_SELECTION


def _broken(task, breakage):
    if breakage == "points_not_100":
        items = [i.model_copy(update={"points": 10}) for i in task.rubric.items]
        return task.model_copy(update={"rubric": task.rubric.model_copy(update={"items": items})})
    if breakage == "foreign_competency":
        return task.model_copy(update={"competency_ids": ["C9"]})
    if breakage == "no_process_item":
        edited = task.model_dump(mode="json")
        for item in edited["rubric"]["items"]:
            item["type"] = "output"
        return edited
    return task.model_copy(update={"instruction": "  "})


@pytest.mark.parametrize(
    "breakage", ["points_not_100", "foreign_competency", "no_process_item", "empty_instruction"]
)
def test_an_edited_version_that_breaks_a_domain_rule_is_rejected(
    service, awaiting_review, breakage
):
    edited = _broken(awaiting_review.task, breakage)

    with pytest.raises(InvalidEditedTask):
        service.review(awaiting_review.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM, edited)

    after = service.get_draft(awaiting_review.id)
    assert after.state is DraftState.AWAITING_REVIEW
    assert after.review is None


@pytest.mark.parametrize(
    "breakage", ["points_not_100", "foreign_competency", "no_process_item", "empty_instruction"]
)
def test_rejecting_stores_the_edited_version_as_given_without_validating_it(
    service, awaiting_review, breakage
):
    edited = _broken(awaiting_review.task, breakage)

    service.review(awaiting_review.id, REVIEWER, ALL_KEEP, ReviewDecision.REJECT, edited)

    after = service.get_draft(awaiting_review.id)
    assert after.state is DraftState.REJECTED
    assert after.review.edited_task == edited


def test_a_confirmation_that_fails_midway_leaves_the_draft_awaiting_review(
    service, awaiting_review, fake_gateway, monkeypatch
):
    def embedding_outage(*args, **kwargs):
        raise ConnectionError("embedding service down")

    monkeypatch.setattr(fake_gateway, "embed", embedding_outage)

    with pytest.raises(ConnectionError):
        service.review(awaiting_review.id, REVIEWER, ALL_KEEP, ReviewDecision.CONFIRM)

    after = service.get_draft(awaiting_review.id)
    assert after.state is DraftState.AWAITING_REVIEW
    assert after.review is None
