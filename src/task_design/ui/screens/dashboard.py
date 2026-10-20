"""대시보드: 성공 기준 2개, 검토 라벨 분포, 총 작업 시간·비용 per draft, trace links."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from task_design.domain import LABELED_COMPONENTS, Metrics, ReviewLabel
from task_design.ui.screens.common import call

KST = ZoneInfo("Asia/Seoul")
# Ordinal labels → one hue, light (유지) to dark (전면 재작성).
LABEL_COLORS = ["#9ec5f4", "#4a8fe0", "#1f4e99"]


def period_of(start: date, end: date) -> dict[str, datetime]:
    """Calendar days in KST, end day inclusive (→ exclusive midnight after it)."""
    return {"start": datetime.combine(start, time.min, KST),
            "end": datetime.combine(end + timedelta(days=1), time.min, KST)}


def render(service) -> None:
    st.header("대시보드")
    today = datetime.now(KST).date()
    picked = st.date_input("기간", (today - timedelta(days=29), today), format="YYYY.MM.DD",
                           key="dash-period")
    if not isinstance(picked, tuple) or len(picked) != 2:
        st.info("기간의 시작일과 종료일을 모두 고르세요.")
        return
    m: Metrics | None = call(service.metrics, period_of(*picked))
    if m is None:
        return
    _targets(m)
    st.divider()
    _labels(m)
    st.divider()
    _work_times(m)
    st.divider()
    _costs(m)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _verdict(ok: bool | None) -> str:
    return "데이터 없음" if ok is None else ("목표 달성" if ok else "목표 미달")


def _targets(m: Metrics) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("전면 재작성 없이 통과한 비율", _pct(m.pass_without_full_rewrite_ratio),
              help=f"목표 {m.pass_ratio_target * 100:.0f}% 이상. 검토한 초안 중 전면 재작성 라벨 없이 "
                   "확정된 초안의 비율 (반려는 실패로 셉니다).")
    c1.caption(f"목표 {m.pass_ratio_target * 100:.0f}% · {_verdict(m.meets_pass_ratio_target)}")
    work = m.mean_total_work_minutes
    c2.metric("과제 1개당 총 작업 시간", "—" if work is None else f"{work:.1f}분",
              help="사람 시간(탭이 보이고 활동 중인 구간만) + 에이전트 대기 시간의 평균.")
    c2.caption(f"목표 {m.work_minutes_target:.0f}분 이하 · {_verdict(m.meets_work_minutes_target)}")
    c3.metric("검토한 초안", f"{m.reviewed_drafts}개", help="기간 안에 최종 검토(확정·반려)된 초안")
    c3.caption(f"통과 {m.passed_drafts}개")
    c4.metric("LLM 비용 (추정)", f"${m.total_cost_usd:.4f}")
    c4.caption(f"호출 {m.total_calls:,}회 · 기간 안에 만든 초안 {len(m.draft_costs)}개")


def _labels(m: Metrics) -> None:
    st.subheader("구성요소별 검토 라벨 분포")
    rows = [{"구성요소": comp.ko, "검토 라벨": label.ko,
             "건수": m.label_distribution.get(comp, {}).get(label, 0)}
            for comp in LABELED_COMPONENTS for label in ReviewLabel]
    df = pd.DataFrame(rows)
    if df["건수"].sum() == 0:
        st.info("이 기간에 검토된 초안이 없습니다.")
        return
    components = [c.ko for c in LABELED_COMPONENTS]
    labels = [lb.ko for lb in ReviewLabel]
    chart = alt.Chart(df).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
        x=alt.X("구성요소:N", sort=components, title="구성요소", axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("검토 라벨:N", sort=labels),
        y=alt.Y("건수:Q", title="건수", axis=alt.Axis(tickMinStep=1)),
        color=alt.Color("검토 라벨:N", sort=labels, title="검토 라벨",
                        scale=alt.Scale(domain=labels, range=LABEL_COLORS)),
        tooltip=[alt.Tooltip("구성요소:N"), alt.Tooltip("검토 라벨:N"), alt.Tooltip("건수:Q")],
    )
    st.altair_chart(chart, width="stretch")
    table = df.pivot(index="구성요소", columns="검토 라벨", values="건수")
    table = table.reindex(index=[c.ko for c in LABELED_COMPONENTS],
                          columns=[lb.ko for lb in ReviewLabel])
    st.dataframe(table, width="stretch")


def _work_times(m: Metrics) -> None:
    st.subheader("초안별 총 작업 시간")
    if not m.work_times:
        st.info("이 기간에 검토된 초안이 없습니다.")
        return
    df = pd.DataFrame([{
        "초안": str(w.draft_id)[:8], "검토 시각": w.reviewed_at.astimezone(KST),
        "결정": w.decision.ko, "통과": "✓" if w.passed else "✗",
        "사람 시간(분)": round(w.human_minutes, 1), "에이전트 시간(분)": round(w.agent_minutes, 1),
        "총 작업 시간(분)": round(w.total_minutes, 1), "추적": w.trace_url,
    } for w in m.work_times])
    st.dataframe(df, hide_index=True, width="stretch", column_config=_link_config())
    _trace_hint(df)


def _costs(m: Metrics) -> None:
    st.subheader("초안별 LLM 비용과 호출 수")
    if not m.draft_costs:
        st.info("이 기간에 만든 초안이 없습니다.")
        return
    df = pd.DataFrame([{
        "초안": str(c.draft_id)[:8], "생성 시각": c.created_at.astimezone(KST),
        "호출 수": c.calls, "입력 토큰": c.input_tokens, "출력 토큰": c.output_tokens,
        "비용(USD)": round(c.estimated_cost_usd, 4), "추적": c.trace_url,
    } for c in m.draft_costs])
    st.dataframe(df, hide_index=True, width="stretch", column_config=_link_config())
    with st.expander("역할별 비용"):
        roles = pd.DataFrame([{"역할": c.role, "호출 수": c.calls,
                               "입력 토큰": c.input_tokens, "출력 토큰": c.output_tokens,
                               "비용(USD)": c.estimated_cost_usd} for c in m.costs])
        st.dataframe(roles.groupby("역할", as_index=False).sum(), hide_index=True,
                     width="stretch")


def _link_config() -> dict:
    return {
        "추적": st.column_config.LinkColumn("추적", display_text="Langfuse에서 열기"),
        "검토 시각": st.column_config.DatetimeColumn("검토 시각", format="YYYY.MM.DD HH:mm"),
        "생성 시각": st.column_config.DatetimeColumn("생성 시각", format="YYYY.MM.DD HH:mm"),
    }


def _trace_hint(df: pd.DataFrame) -> None:
    if df["추적"].isna().all():
        st.caption("추적 링크를 보려면 TD_LANGFUSE_PROJECT_ID(와 필요하면 TD_LANGFUSE_PUBLIC_HOST)를 "
                   "설정하세요.")
