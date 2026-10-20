"""Korean user-facing text for errors (everything else is written inline in the screens)."""

from __future__ import annotations

from task_design.domain import DraftErrorKind
from task_design.service import (
    DraftNotFound,
    InvalidActivityInterval,
    InvalidDraftInput,
    InvalidDraftState,
    InvalidEditedTask,
    InvalidPeriod,
    InvalidReviewLabels,
    SelfReview,
    UnknownCandidate,
    UnknownCompetency,
    UnknownStaff,
)

# Service errors whose message is already Korean and meant for the user.
USER_ERRORS: tuple[type[Exception], ...] = (
    InvalidDraftInput, UnknownCompetency, SelfReview, InvalidReviewLabels, InvalidEditedTask,
    UnknownStaff, InvalidActivityInterval, InvalidPeriod, DraftNotFound, InvalidDraftState,
    UnknownCandidate,
)

GENERIC_ERROR = "요청을 처리하지 못했습니다. 잠시 뒤 다시 시도하세요."

_HEAD = "생성 중 오류가 발생했습니다."

# Draft.error_kind → what the 다시 실행 panel says (the raw text goes in an expander).
RUN_ERRORS: dict[DraftErrorKind, str] = {
    DraftErrorKind.INTERRUPTED:
        "앱이 다시 시작되어 진행 중이던 실행이 중단되었습니다. 다시 실행하면 이어서 진행합니다.",
    DraftErrorKind.CANDIDATES_EXHAUSTED:
        "기존 과제나 이미 보여 준 후보와 겹치지 않는 상황 후보 3개를 만들지 못했습니다. "
        "다시 실행하면 새 후보를 다시 만듭니다. 계속 실패하면 메모로 방향을 바꿔 새 초안을 만드세요.",
    DraftErrorKind.INVALID_MODEL_OUTPUT:
        f"{_HEAD} 모델 응답이 형식에 맞지 않아 단계를 마치지 못했습니다. 다시 실행해 보세요.",
    DraftErrorKind.MODEL_UNAVAILABLE:
        f"{_HEAD} 모델 서비스에 연결하지 못했거나 응답이 늦었습니다. 잠시 뒤 다시 실행하세요.",
    DraftErrorKind.CONFIGURATION: f"{_HEAD} 모델 라우팅 설정을 확인하세요.",
    DraftErrorKind.UNEXPECTED:
        f"{_HEAD} 다시 실행해도 같은 오류가 나면 아래 상세 내용을 개발자에게 전달하세요.",
}


def error_text(exc: Exception) -> str:
    """What to show for an exception from a service call."""
    if isinstance(exc, USER_ERRORS):
        return str(exc)
    return GENERIC_ERROR


def run_error_text(kind: DraftErrorKind | None) -> str:
    """Korean explanation of a failed run, by ``Draft.error_kind``."""
    return RUN_ERRORS[kind or DraftErrorKind.UNEXPECTED]
