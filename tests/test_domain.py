import pytest
from pydantic import ValidationError

from task_design.domain import (
    Component,
    DraftInput,
    Review,
    ReviewDecision,
    ReviewLabel,
    Rubric,
    RubricItem,
    RubricItemType,
)


def item(type_: RubricItemType) -> RubricItem:
    return RubricItem(
        title="함정 데이터 검증",
        competency_id="C1",
        type=type_,
        points=10,
        criteria_zero="검증하지 않음",
        criteria_partial="일부 검증",
        criteria_full="모든 함정을 찾아 수정",
    )


P, O = RubricItemType.PROCESS, RubricItemType.OUTPUT


@pytest.mark.parametrize("types", [[P, O, O], [P, P, O, O, O]])
def test_rubric_with_3_to_5_items_and_a_process_item_is_valid(types):
    assert Rubric(items=[item(t) for t in types]).total_points == 10 * len(types)


@pytest.mark.parametrize(
    "types", [[P, O], [P, O, O, O, O, O], [O, O, O]], ids=["too few", "too many", "no process"]
)
def test_rubric_shape_violations_are_rejected(types):
    with pytest.raises(ValidationError):
        Rubric(items=[item(t) for t in types])


def test_draft_input_takes_one_or_two_competencies():
    base = {"audience": {"job": "마케터", "career_level": "3년차"}, "difficulty": "high"}
    DraftInput(competency_ids=["C1", "C2"], **base)
    with pytest.raises(ValidationError):
        DraftInput(competency_ids=[], **base)
    with pytest.raises(ValidationError):
        DraftInput(competency_ids=["C1", "C2", "C3"], **base)


def test_review_requires_labels_for_scenario_instruction_and_rubric():
    with pytest.raises(ValidationError):
        Review(
            reviewer_id=2,
            labels={Component.SCENARIO: ReviewLabel.KEEP, Component.RUBRIC: ReviewLabel.KEEP},
            decision=ReviewDecision.CONFIRM,
        )
