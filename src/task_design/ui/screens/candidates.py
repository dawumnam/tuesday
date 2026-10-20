"""상황 후보 선택: 3 candidates with their 유사 과제, select one or regenerate."""

from __future__ import annotations

import streamlit as st

from task_design.domain import Draft
from task_design.stepper import Stepper
from task_design.ui.screens import stepping
from task_design.ui.screens.common import call


def render(service, draft: Draft, *, stepper: Stepper | None = None) -> None:
    st.subheader("상황 후보 선택")
    st.caption("마음에 드는 상황을 하나 고르면 지시문·첨부 자료·채점 기준을 만들고 모의 응시로 검증합니다. "
               "고른 상황은 이후 자기 수정 루프에서 바뀌지 않습니다.")
    similar = {t.id: t for t in draft.similar_tasks}
    cols = st.columns(len(draft.candidates) or 1)
    for n, (col, cand) in enumerate(zip(cols, draft.candidates, strict=False), start=1):
        with col, st.container(border=True):
            st.markdown(f"**후보 {n}. {cand.title}**")
            st.write(cand.scenario)
            refs = [similar[r] for r in cand.referenced_task_ids if r in similar]
            with st.expander(f"참고한 유사 과제 {len(refs)}개"):
                if not refs:
                    st.caption("참고한 유사 과제가 없습니다.")
                for t in refs:
                    st.markdown(f"- {t.scenario[:160]}{'…' if len(t.scenario) > 160 else ''}")
                    st.caption(f"지시문: {t.instruction[:120]}"
                               f"{'…' if len(t.instruction) > 120 else ''}")
            if st.button("이 상황으로 진행", key=f"pick-{cand.id}", type="primary",
                         width="stretch"):
                if call(service.select_scenario, draft.id, cand.id) is not None:
                    st.rerun()
    if stepper is not None:  # 데모 모드: how the 유사 과제 and these 후보 were found
        stepping.show_traces(stepper, draft)
    st.divider()
    st.caption("마음에 드는 후보가 없으면 이전에 보인 것과 다른 후보 3개를 다시 받을 수 있습니다.")
    if st.button("후보 다시 받기", key=f"regen-{draft.id}"):
        if call(service.regenerate_scenarios, draft.id) is not None:
            st.rerun()
