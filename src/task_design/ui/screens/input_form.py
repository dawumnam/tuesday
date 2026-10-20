"""입력 폼: 역량 1~2개, 대상자 (직무·경력 수준), 목표 난이도, 메모 → ``service.start``."""

from __future__ import annotations

import streamlit as st

from task_design.domain import Level
from task_design.ui.screens.common import PAGE_DRAFT, call, go

CAREER_LEVELS = ["신입", "3년차", "5년차", "10년차 팀장"]
LEVELS = [Level.HIGH, Level.MEDIUM, Level.LOW]


def render(service, *, staff_id: int, demo: bool, on_started) -> None:
    st.header("새 초안 만들기")
    st.caption("역량·대상자·목표 난이도를 고르면 에이전트가 기존 과제를 참고해 상황 후보 3개를 제안합니다.")
    if demo:
        st.info(
            f"데모 모드입니다. 실제 모델 대신 미리 준비한 한국어 예시로 응답합니다. "
            f"난이도 '상'을 고르면 자기 수정 루프 1회를, 메모에 '{_fail_marker()}'라고 적으면 "
            "실패 리포트를 볼 수 있습니다.",
            icon="🧪",
        )
    competencies = call(service.list_competencies) or []
    if not competencies:
        st.warning("역량 체계가 비어 있습니다. `task-design seed`로 역량 체계와 과제를 먼저 적재하세요.")
    names = {c.id: c.name for c in competencies}

    with st.form("new-draft"):
        competency_ids = st.multiselect(
            "평가 역량 (1~2개)", options=list(names), format_func=lambda c: f"{c} {names[c]}",
            max_selections=2, placeholder="역량을 고르세요",
        )
        col1, col2 = st.columns(2)
        job = col1.text_input("대상자 직무", placeholder="예: 마케팅, 재무·회계, 영업 관리")
        career = col2.selectbox("대상자 경력 수준", CAREER_LEVELS, index=1,
                                accept_new_options=True)
        difficulty = st.radio("목표 난이도", LEVELS, index=1, format_func=lambda lv: lv.ko,
                              horizontal=True)
        memo = st.text_area("메모 (선택)", placeholder="정형 입력으로 전하기 어려운 의도를 적으세요.")
        submitted = st.form_submit_button("상황 후보 받기", type="primary")

    if submitted:
        draft = call(service.start, {
            "competency_ids": competency_ids,
            "audience": {"job": job.strip(), "career_level": (career or "").strip()},
            "difficulty": difficulty,
            "memo": memo.strip() or None,
        }, author_id=staff_id)
        if draft is not None:
            on_started(draft.id)
            go(PAGE_DRAFT, draft.id)


def _fail_marker() -> str:
    from task_design.demo import FAIL_MARKER

    return FAIL_MARKER
