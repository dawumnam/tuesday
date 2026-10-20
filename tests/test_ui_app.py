"""Smoke tests per screen: the Streamlit app (AppTest) over the 데모 모드 gateway.

The app runs against a test service (inline runner, so runs finish before the
script continues) installed with ``runtime.use_service``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from task_design.costs import CostStore
from task_design.demo import FAIL_MARKER, build_demo_gateway
from task_design.domain import Component, DraftState, ReviewLabel
from task_design.service import TaskDesignService
from task_design.testing.workflow import make_input, seed_similar_tasks
from task_design.ui import runtime

APP = Path(__file__).resolve().parents[1] / "src" / "task_design" / "ui" / "app.py"


@pytest.fixture
def demo(db, settings):
    gateway = build_demo_gateway(settings, usage_sink=CostStore(db), delay=0)
    seed_similar_tasks(db, gateway, settings)
    service = TaskDesignService(db, gateway, settings)
    runtime.use_service(service, demo=True)
    yield service
    runtime.use_service(None)


def open_app(*, staff: int = 1, draft=None) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.query_params["staff"] = str(staff)
    if draft is not None:
        at.query_params["draft"] = str(draft)
    at.run()
    assert not at.exception, at.exception
    return at


def texts(at: AppTest) -> str:
    parts = [e.value for e in (*at.markdown, *at.caption, *at.error, *at.warning, *at.info,
                               *at.success, *at.header, *at.subheader)]
    parts += [f"{m.label} {m.value}" for m in at.metric]
    return "\n".join(str(p) for p in parts)


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def go_to(at: AppTest, page: str) -> AppTest:
    at.sidebar.radio(key="page").set_value(page).run()
    assert not at.exception, at.exception
    return at


# --- 입력 폼 ------------------------------------------------------------------------


def test_input_form_shows_korean_errors_and_starts_a_draft(demo):
    at = go_to(open_app(), "새 초안")
    assert "새 초안 만들기" in texts(at)
    assert "데모 모드" in texts(at)

    button(at, "상황 후보 받기").click().run()
    assert any("역량" in e.value for e in at.error), "no Korean error for 0 역량"

    at.multiselect[0].select("C1")
    at.text_input[0].input("마케팅")
    button(at, "상황 후보 받기").click().run()

    assert not at.exception
    assert at.session_state["page"] == "초안 보기"
    assert len([b for b in at.button if b.label == "이 상황으로 진행"]) == 3
    assert "상황 후보 선택" in texts(at)


# --- 상황 후보 선택 ----------------------------------------------------------------------


def test_candidate_screen_regenerates_and_selects(demo):
    draft = demo.start(make_input(), author_id=1)
    at = open_app(draft=draft.id)
    assert "참고한 유사 과제" in "\n".join(e.label for e in at.expander)

    button(at, "후보 다시 받기").click().run()
    again = demo.get_draft(draft.id)
    assert {c.id for c in again.candidates}.isdisjoint({c.id for c in draft.candidates})

    at.button(key=f"pick-{again.candidates[0].id}").click().run()

    assert not at.exception
    assert demo.get_draft(draft.id).state is DraftState.AWAITING_REVIEW


# --- 진행 ------------------------------------------------------------------------


def _running_draft(demo, db, settings):
    """A draft whose verification job is (still) running elsewhere."""
    draft = demo.start(make_input(), author_id=1)
    elsewhere = TaskDesignService(db, demo.gateway, settings, run_graph=lambda job: None)
    elsewhere.select_scenario(draft.id, draft.candidates[0].id)
    return draft


def test_progress_screen_shows_round_stage_and_personas(demo, db, settings):
    draft = _running_draft(demo, db, settings)

    at = open_app(draft=draft.id)

    shown = texts(at)
    assert "1회차 검증 중" in shown
    assert "상 수준" in shown and "하 수준" in shown
    assert "지시문 작성" in shown


def test_failed_run_shows_korean_message_and_retries(demo, db, settings):
    draft = _running_draft(demo, db, settings)
    demo.recover_interrupted_runs()  # as after an app restart

    at = open_app(draft=draft.id)
    assert any("중단" in e.value for e in at.error)
    assert any(e.label.startswith("오류 상세") for e in at.expander)

    button(at, "다시 실행").click().run()

    assert not at.exception
    assert demo.get_draft(draft.id).state is DraftState.AWAITING_REVIEW


# --- 검토 ------------------------------------------------------------------------


def _awaiting_review(demo, *, memo=None, author=1):
    draft = demo.start(make_input(memo=memo), author_id=author)
    return demo.select_scenario(draft.id, draft.candidates[0].id)


def test_author_cannot_review_own_draft(demo):
    draft = _awaiting_review(demo)

    at = open_app(staff=1, draft=draft.id)

    assert any("자신이 작성한 초안" in w.value for w in at.warning)
    assert not [b for b in at.button if b.label == "확정"]


def test_reviewer_must_label_before_confirming(demo):
    draft = _awaiting_review(demo)
    at = open_app(staff=2, draft=draft.id)
    assert "모의 응시 점수표" in texts(at)
    assert button(at, "확정").disabled and button(at, "반려").disabled

    for comp, label in (("scenario", ReviewLabel.KEEP), ("instruction", ReviewLabel.PARTIAL_EDIT),
                        ("rubric", ReviewLabel.KEEP)):
        at.radio(key=f"rv-{draft.id}-label-{comp}").set_value(label)
    at.run()
    assert not button(at, "확정").disabled
    at.text_area(key=f"rv-{draft.id}-instruction").input("검토자가 다듬은 지시문입니다.")
    at.run()
    button(at, "확정").click().run()

    assert not at.exception, at.exception
    done = demo.get_draft(draft.id)
    assert done.state is DraftState.CONFIRMED
    assert done.review.reviewer_id == 2
    assert done.review.edited_task.instruction == "검토자가 다듬은 지시문입니다."
    assert done.review.labels[Component.INSTRUCTION] is ReviewLabel.PARTIAL_EDIT
    assert any("확정" in s.value for s in at.success)


def test_review_screen_shows_the_failure_report(demo):
    draft = _awaiting_review(demo, memo=FAIL_MARKER)

    at = open_app(staff=2, draft=draft.id)

    assert any("실패 리포트" in w.value for w in at.warning)
    assert [t.label for t in at.tabs] == ["1회차", "2회차", "3회차", "4회차"]


# --- 초안 목록 · 대시보드 -------------------------------------------------------------


def test_draft_list_shows_my_drafts_and_my_review_queue(demo):
    waiting = _awaiting_review(demo, author=1)

    at = go_to(open_app(staff=2), "초안 목록")

    assert "내가 검토할 초안" in texts(at)
    at.button(key=f"q-open-{waiting.id}").click().run()
    assert at.session_state["page"] == "초안 보기"
    assert at.session_state["draft_id"] == str(waiting.id)


def test_dashboard_shows_success_criteria_in_korean(demo):
    draft = _awaiting_review(demo)
    demo.review(draft.id, 2, {"scenario": "keep", "instruction": "partial_edit",
                              "rubric": "keep"}, "confirm")

    at = go_to(open_app(), "대시보드")

    shown = texts(at)
    assert "전면 재작성 없이 통과한 비율" in shown
    assert "과제 1개당 총 작업 시간" in shown
    assert "구성요소별 검토 라벨 분포" in shown
    assert "초안별 LLM 비용과 호출 수" in shown


# --- 시작 실패 ----------------------------------------------------------------------


def test_missing_api_key_shows_a_korean_hint_instead_of_a_traceback(settings, monkeypatch):
    for name in ("TD_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "TD_FAKE_GATEWAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TD_DATABASE_URL", settings.database_url)
    runtime.use_service(None)
    runtime._build.clear()

    at = AppTest.from_file(str(APP), default_timeout=30).run()

    assert not at.exception
    assert any("TD_FAKE_GATEWAY=1" in e.value for e in at.error)


def test_rejecting_without_edits_stores_no_edited_copy(demo):
    draft = _awaiting_review(demo)
    at = open_app(staff=2, draft=draft.id)
    for comp in ("scenario", "instruction", "rubric"):
        at.radio(key=f"rv-{draft.id}-label-{comp}").set_value(ReviewLabel.FULL_REWRITE)
    at.run()

    button(at, "반려").click().run()

    done = demo.get_draft(draft.id)
    assert done.state is DraftState.REJECTED
    assert done.review.edited_task is None
