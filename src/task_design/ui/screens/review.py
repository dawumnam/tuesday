"""검토: one screen to edit 상황·지시문·첨부 자료·채점 기준, with the 검증 근거 alongside.

검토 라벨 for 상황·지시문·채점 기준 must all be set before 확정 / 반려 are enabled.
The author sees the draft read-only (교차 검토: someone else must review it).
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd
import streamlit as st

from task_design.domain import (
    LABELED_COMPONENTS,
    AttachmentFormat,
    Draft,
    ReviewDecision,
    ReviewLabel,
    RubricItemType,
    Task,
)
from task_design.ui.screens.common import call, show_evidence, show_task, staff_name

_TYPES = {t.ko: t for t in RubricItemType}
_FORMATS = list(AttachmentFormat)


def render(service, draft: Draft, *, staff_id: int, staff: dict[int, str]) -> None:
    if staff_id == draft.author_id:
        st.warning(
            "자신이 작성한 초안은 최종 검토할 수 없습니다. "
            f"다른 담당자({_others(staff, staff_id)})가 검토해야 합니다.", icon="🔒")
        left, right = st.columns([3, 2], gap="large")
        with left:
            show_task(draft.task, key=f"ro-{draft.id}")
        with right:
            show_evidence(draft, key=f"ev-{draft.id}")
        return

    st.subheader("최종 검토")
    st.caption("고칠 부분은 바로 고치고, 상황·지시문·채점 기준마다 검토 라벨을 붙인 뒤 확정 또는 반려하세요.")
    k = f"rv-{draft.id}"
    left, right = st.columns([3, 2], gap="large")
    with right:
        st.markdown("### 검증 근거")
        show_evidence(draft, key=f"ev-{draft.id}")
    with left:
        edited, problem = _editor(draft, k)
        st.markdown("### 검토 라벨")
        labels = {}
        for comp in LABELED_COMPONENTS:
            labels[comp] = st.radio(
                comp.ko, list(ReviewLabel), index=None, horizontal=True,
                format_func=lambda lb: lb.ko, key=f"{k}-label-{comp.value}",
            )
        ready = all(v is not None for v in labels.values())
        if not ready:
            st.caption("상황·지시문·채점 기준에 검토 라벨을 모두 붙여야 확정하거나 반려할 수 있습니다.")
        c1, c2 = st.columns(2)
        confirm = c1.button("확정", type="primary", disabled=not ready, key=f"{k}-confirm",
                            width="stretch")
        reject = c2.button("반려", disabled=not ready, key=f"{k}-reject", width="stretch")
        if confirm or reject:
            if confirm and problem:  # 반려 stores the 수정본 as it is
                st.error(problem)
                return
            decision = ReviewDecision.CONFIRM if confirm else ReviewDecision.REJECT
            unchanged = (isinstance(edited, Task) and edited.model_dump(mode="json")
                         == draft.task.model_dump(mode="json"))
            done = call(service.review, draft.id, staff_id, labels, decision,
                        None if unchanged else edited)
            if done is not None:
                st.toast(f"초안을 {decision.ko}했습니다.")
                st.rerun()


def _others(staff: dict[int, str], staff_id: int) -> str:
    return ", ".join(staff_name(staff, s) for s in staff if s != staff_id) or "다른 담당자"


def _editor(draft: Draft, k: str) -> tuple[dict | Task, str | None]:
    """Widgets for every part of the task; returns (수정본, problem text or None)."""
    task = draft.task
    scenario = st.text_area("상황", task.scenario, height=160, key=f"{k}-scenario")
    instruction = st.text_area("지시문", task.instruction, height=140, key=f"{k}-instruction")

    st.markdown("**첨부 자료**")
    attachments = []
    for i, a in enumerate(task.attachments):
        with st.expander(f"📎 {a.filename} ({a.format.ko})", expanded=False):
            c1, c2 = st.columns([2, 1])
            filename = c1.text_input("파일 이름", a.filename, key=f"{k}-att{i}-name")
            fmt = c2.selectbox("형식", _FORMATS, index=_FORMATS.index(a.format),
                               format_func=lambda f: f.ko, key=f"{k}-att{i}-format")
            content = st.text_area("내용", a.content, height=180, key=f"{k}-att{i}-content")
            traps = st.data_editor(
                pd.DataFrame([{"위치": t.location, "내용": t.description} for t in a.traps],
                             columns=["위치", "내용"]),
                num_rows="dynamic", hide_index=True, width="stretch", key=f"{k}-att{i}-traps",
            )
            attachments.append({
                "filename": filename, "format": fmt.value, "content": content,
                "traps": [{"location": _s(r["위치"]), "description": _s(r["내용"])}
                          for r in traps.to_dict("records") if _s(r["위치"]) or _s(r["내용"])],
            })

    st.markdown("**채점 기준** (항목 3~5개, 배점 합계 100점, 과정 항목 1개 이상)")
    ids = list(draft.input.competency_ids)
    rubric = st.data_editor(
        pd.DataFrame([{
            "항목": it.title, "유형": it.type.ko, "역량": it.competency_id, "배점": it.points,
            "0점": it.criteria_zero, "부분 점수": it.criteria_partial, "만점": it.criteria_full,
        } for it in task.rubric.items]),
        num_rows="dynamic", hide_index=True, width="stretch", key=f"{k}-rubric",
        column_config={
            "유형": st.column_config.SelectboxColumn("유형", options=list(_TYPES), required=True),
            "역량": st.column_config.SelectboxColumn("역량", options=ids, required=True),
            "배점": st.column_config.NumberColumn("배점", min_value=1, max_value=100, step=1,
                                                required=True),
        },
    )
    items, problem = [], None
    for r in rubric.to_dict("records"):
        if not any(_s(v) for v in r.values()):
            continue
        points = r.get("배점")
        if points is None or (isinstance(points, float) and math.isnan(points)):
            problem = "채점 기준의 모든 항목에 배점을 넣어야 합니다."
            points = 0
        items.append({
            "title": _s(r["항목"]), "type": _TYPES.get(_s(r["유형"]), RubricItemType.OUTPUT).value,
            "competency_id": _s(r["역량"]), "points": int(points),
            "criteria_zero": _s(r["0점"]), "criteria_partial": _s(r["부분 점수"]),
            "criteria_full": _s(r["만점"]),
        })
    total = sum(i["points"] for i in items)
    st.caption(f"배점 합계: {total}점")
    edited = task.model_dump(mode="json") | {
        "scenario": scenario, "instruction": instruction, "attachments": attachments,
        "rubric": {"items": items},
    }
    try:
        return Task.model_validate(edited), problem
    except Exception:
        # The service explains what is wrong (InvalidEditedTask, in Korean).
        return edited, problem


def _s(value: Any) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()
