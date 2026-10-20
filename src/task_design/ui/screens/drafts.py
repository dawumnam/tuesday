"""초안 목록 (my drafts, the 검토 대기 queue) and 초안 보기 (screen chosen by state)."""

from __future__ import annotations

from uuid import UUID

import streamlit as st

from task_design.domain import Draft, DraftState, DraftSummary
from task_design.ui.screens import candidates, progress, review
from task_design.ui.screens.common import (
    PAGE_DRAFT,
    PAGE_NEW,
    STATE_ICON,
    call,
    go_callback,
    show_evidence,
    show_task,
    staff_name,
)
from task_design.ui.texts import run_error_text
from task_design.workflow import DraftNotFound


def render_list(service, *, staff_id: int, staff: dict[int, str]) -> None:
    st.header("초안 목록")
    queue = [d for d in call(service.list_drafts, states=[DraftState.AWAITING_REVIEW]) or []
             if d.author_id != staff_id]
    st.subheader(f"내가 검토할 초안 ({len(queue)})")
    if not queue:
        st.caption("다른 담당자가 작성한 검토 대기 초안이 없습니다.")
    for d in queue:
        _row(d, staff, prefix="q")

    mine = call(service.list_drafts, author_id=staff_id) or []
    st.subheader(f"내가 작성한 초안 ({len(mine)})")
    if not mine:
        st.caption("아직 작성한 초안이 없습니다.")
        st.button("새 초안 만들기", on_click=go_callback(PAGE_NEW))
    for d in mine:
        _row(d, staff, prefix="m")

    st.divider()
    with st.form("open-by-id"):
        raw = st.text_input("초안 ID로 열기", placeholder="예: 3f2a…-…")
        if st.form_submit_button("열기"):
            try:
                draft_id = UUID(raw.strip())
            except ValueError:
                st.error("초안 ID 형식이 올바르지 않습니다.")
            else:
                st.session_state["_goto"] = (PAGE_DRAFT, str(draft_id))
                st.rerun()


def _row(d: DraftSummary, staff: dict[int, str], *, prefix: str) -> None:
    with st.container(border=True):
        c1, c2, c3 = st.columns([6, 3, 1], vertical_alignment="center")
        c1.markdown(f"**{d.title}**")
        c1.caption(f"{', '.join(d.input.competency_ids)} · {d.input.audience.job} "
                   f"{d.input.audience.career_level} · 난이도 {d.input.difficulty.ko} · "
                   f"작성자 {staff_name(staff, d.author_id)}")
        status = f"{STATE_ICON.get(d.state, '')} {d.state.ko}"
        if d.stage:
            status += f" · {d.stage.ko}"
        if d.has_error:
            status += " · ⚠️ 오류"
        c2.markdown(status)
        c2.caption(f"수정 {d.updated_at.astimezone().strftime('%m.%d %H:%M')}")
        c3.button("열기", key=f"{prefix}-open-{d.id}", on_click=go_callback(PAGE_DRAFT, d.id))


def render_draft(service, draft_id: str | None, *, staff_id: int, staff: dict[int, str]) -> Draft | None:
    if not draft_id:
        st.info("열린 초안이 없습니다. '초안 목록'에서 초안을 고르거나 새 초안을 만드세요.")
        return None
    try:
        draft = service.get_draft(UUID(draft_id))
    except (ValueError, DraftNotFound):
        st.error("초안을 찾을 수 없습니다.")
        return None

    title = next((c.title for c in draft.candidates if c.id == draft.selected_candidate_id),
                 None) or "새 초안"
    st.header(title)
    st.caption(
        f"{STATE_ICON.get(draft.state, '')} {draft.state.ko} · 작성자 {staff_name(staff, draft.author_id)} · "
        f"{', '.join(draft.input.competency_ids)} · {draft.input.audience.job} "
        f"{draft.input.audience.career_level} · 난이도 {draft.input.difficulty.ko} · ID {draft.id}"
    )
    if draft.input.memo:
        st.caption(f"메모: {draft.input.memo}")

    if draft.error:
        _error_panel(service, draft)
        if draft.state in (DraftState.GENERATING, DraftState.VERIFYING):
            progress.show_progress(draft)
            return draft

    match draft.state:
        case DraftState.GENERATING | DraftState.VERIFYING:
            progress.render(service, draft)
        case DraftState.AWAITING_SCENARIO_SELECTION:
            candidates.render(service, draft)
        case DraftState.AWAITING_REVIEW:
            review.render(service, draft, staff_id=staff_id, staff=staff)
        case DraftState.CONFIRMED | DraftState.REJECTED:
            _reviewed(draft, staff)
    return draft


def _error_panel(service, draft: Draft) -> None:
    st.error(run_error_text(draft.error), icon="⚠️")
    if st.button("재시도", key=f"retry-{draft.id}", type="primary"):
        if call(service.retry, draft.id) is not None:
            st.rerun()
    with st.expander("오류 상세 (개발자용)"):
        st.code(draft.error, language=None, wrap_lines=True)


def _reviewed(draft: Draft, staff: dict[int, str]) -> None:
    rv = draft.review
    if rv is not None:
        labels = " · ".join(f"{c.ko} {lb.ko}" for c, lb in rv.labels.items())
        when = rv.reviewed_at.astimezone().strftime("%Y.%m.%d %H:%M") if rv.reviewed_at else ""
        box = st.success if draft.state is DraftState.CONFIRMED else st.warning
        box(f"{rv.decision.ko} · 검토자 {staff_name(staff, rv.reviewer_id)} · {when}\n\n검토 라벨: {labels}")
        if draft.state is DraftState.CONFIRMED:
            st.caption("확정 과제는 과제 DB에 들어가 이후 유사 과제 검색에 쓰입니다.")
    task = (rv.edited_task if rv and rv.edited_task else None) or draft.task
    left, right = st.columns([3, 2], gap="large")
    with left:
        if rv and rv.edited_task:
            st.caption("검토자가 수정한 과제입니다.")
        show_task(task, key=f"final-{draft.id}")
    with right:
        show_evidence(draft, key=f"ev-{draft.id}")
