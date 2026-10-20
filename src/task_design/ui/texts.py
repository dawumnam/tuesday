"""Korean user-facing text for errors (everything else is written inline in the screens)."""

from __future__ import annotations

from task_design.service import (
    INTERRUPTED_RUN_ERROR,
    InvalidActivityInterval,
    InvalidDraftInput,
    InvalidEditedTask,
    InvalidPeriod,
    InvalidReviewLabels,
    SelfReview,
    UnknownCompetency,
    UnknownStaff,
)
from task_design.workflow import DraftNotFound, InvalidDraftState
from task_design.workflow.drafts import UnknownCandidate

# Service errors whose message is already Korean and meant for the user.
USER_ERRORS: tuple[type[Exception], ...] = (
    InvalidDraftInput, UnknownCompetency, SelfReview, InvalidReviewLabels, InvalidEditedTask,
    UnknownStaff, InvalidActivityInterval, InvalidPeriod, DraftNotFound, InvalidDraftState,
    UnknownCandidate,
)

GENERIC_ERROR = "요청을 처리하지 못했습니다. 잠시 뒤 다시 시도하세요."


def error_text(exc: Exception) -> str:
    """What to show for an exception from a service call."""
    if isinstance(exc, USER_ERRORS):
        return str(exc)
    return GENERIC_ERROR


def run_error_text(raw: str) -> str:
    """Korean explanation of ``Draft.error`` (the raw text goes in an expander)."""
    head = "생성 중 오류가 발생했습니다."
    if raw == INTERRUPTED_RUN_ERROR:
        return "앱이 다시 시작되어 진행 중이던 실행이 중단되었습니다. 재시도하면 이어서 진행합니다."
    kind = raw.split(":", 1)[0]
    if kind in {"InvalidModelOutput", "InvalidModelContent", "ValidationError"}:
        return f"{head} 모델 응답이 형식에 맞지 않아 단계를 마치지 못했습니다. 재시도해 보세요."
    if any(word in raw for word in ("Timeout", "timed out", "Connect", "503", "429", "500")):
        return f"{head} 모델 서비스에 연결하지 못했거나 응답이 늦었습니다. 잠시 뒤 재시도하세요."
    if kind == "LookupError":
        return f"{head} 모델 라우팅 설정을 확인하세요."
    return f"{head} 재시도해도 같은 오류가 나면 아래 상세 내용을 개발자에게 전달하세요."
