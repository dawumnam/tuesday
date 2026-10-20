"""Shared pieces: navigation, staff names, read-only 과제 view, 검증 근거 (scores, rounds)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pandas as pd
import streamlit as st

from task_design.domain import (
    Draft,
    DraftRound,
    DraftState,
    FailureReport,
    Level,
    MockExamResult,
    Task,
)
from task_design.ui.texts import error_text

PAGE_NEW = "새 초안"
PAGE_LIST = "초안 목록"
PAGE_DRAFT = "초안 보기"
PAGE_DASHBOARD = "대시보드"
PAGES = [PAGE_NEW, PAGE_LIST, PAGE_DRAFT, PAGE_DASHBOARD]

LEVELS = (Level.HIGH, Level.MEDIUM, Level.LOW)

STATE_ICON = {
    DraftState.GENERATING: "⏳",
    DraftState.AWAITING_SCENARIO_SELECTION: "👉",
    DraftState.VERIFYING: "⏳",
    DraftState.AWAITING_REVIEW: "📝",
    DraftState.CONFIRMED: "✅",
    DraftState.REJECTED: "↩️",
}


def go(page: str, draft_id: UUID | str | None = None) -> None:
    """Switch screen on the next run (safe to call anywhere; reruns immediately)."""
    st.session_state["_goto"] = (page, str(draft_id) if draft_id else None)
    st.rerun()


def go_callback(page: str, draft_id: UUID | str | None = None):
    """``on_click`` version of ``go`` (callbacks run before widgets are created)."""

    def cb() -> None:
        st.session_state["page"] = page
        if draft_id is not None:
            st.session_state["draft_id"] = str(draft_id)

    return cb


def staff_name(staff: dict[int, str], staff_id: int) -> str:
    return staff.get(staff_id, f"담당자 {staff_id}")


def call(fn, *args, **kwargs) -> Any:
    """Run a service call; show a Korean error and return None if it raises."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # shown, never a traceback
        st.error(error_text(e))
        return None


# --- 과제 (read-only) --------------------------------------------------------------


def show_task(task: Task, *, key: str) -> None:
    st.markdown("#### 상황")
    st.write(task.scenario)
    st.markdown("#### 지시문")
    st.write(task.instruction)
    show_attachments(task)
    st.markdown("#### 채점 기준")
    st.dataframe(rubric_frame(task), hide_index=True, width="stretch", key=f"{key}-rubric")


def show_attachments(task: Task) -> None:
    st.markdown("#### 첨부 자료")
    if not task.attachments:
        st.caption("첨부 자료 없음")
    for a in task.attachments:
        with st.expander(f"📎 {a.filename} ({a.format.ko}) · 함정 {len(a.traps)}개"):
            st.code(a.content, language=None, wrap_lines=True)
            for t in a.traps:
                st.markdown(f"- **함정** · {t.location}: {t.description}")


def rubric_frame(task: Task) -> pd.DataFrame:
    return pd.DataFrame([{
        "항목": i.title, "유형": i.type.ko, "역량": i.competency_id, "배점": i.points,
        "0점": i.criteria_zero, "부분 점수": i.criteria_partial, "만점": i.criteria_full,
    } for i in task.rubric.items])


# --- 검증 근거 --------------------------------------------------------------------


def _mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def score_frame(round: DraftRound) -> pd.DataFrame:
    rows = []
    by_level = {r.persona_level: r for r in round.mock_exam_results}
    for level in LEVELS:
        r = by_level.get(level)
        if r is None:
            continue
        rows.append({
            "응시자 페르소나": level.ko,
            "평균 점수": round_(r.mean_score),
            "표준편차": round_(r.score_stdev),
            **{f"채점 {i + 1}": round_(g.total) for i, g in enumerate(r.gradings)},
        })
    return pd.DataFrame(rows)


def round_(x: float) -> float:
    return round(x, 1)


def verdict_line(round: DraftRound) -> str:
    v = round.verdict
    if v is None:
        return "판정 전"
    return (f"풀이 가능성 {_mark(v.solvable)} · 변별력 {_mark(v.discriminating)} · "
            f"채점 일관성 {_mark(v.consistent)}"
            + (f" — 실패: {', '.join(f.ko for f in v.failures)}" if v.failures else " — 통과"))


def show_failure_report(report: FailureReport) -> None:
    failures = ", ".join(f.ko for f in report.failure_types) or "판정 전"
    lines = [f"**실패 리포트** · 실패 유형: {failures}"]
    if report.stopped_by_token_budget and not report.scores_by_round:
        lines.append("모의 응시 전에 토큰 예산을 다 써서 검증하지 못했습니다. 직접 검토하세요.")
    elif report.stopped_by_token_budget:
        lines.append("토큰 예산 초과로 중단되었습니다.")
    else:
        lines.append("자기 수정 루프를 모두 쓰고도 검증에 실패했습니다. 직접 고쳐 쓸지 판단하세요.")
    st.warning("\n\n".join(lines), icon="⚠️")
    if report.scores_by_round:
        table = pd.DataFrame([
            {"회차": f"{n}회차", **{lv.ko: round_(scores.get(lv, float("nan"))) for lv in LEVELS}}
            for n, scores in enumerate(report.scores_by_round, start=1)
        ])
        st.caption("회차별 평균 점수")
        st.dataframe(table, hide_index=True, width="stretch")
    if report.attempted_fixes:
        st.caption("시도한 수정")
        for fix in report.attempted_fixes:
            st.markdown(f"- {fix}")


def show_evidence(draft: Draft, *, key: str) -> None:
    """모의 응시 점수표 + 실패 리포트 + 회차 이력 (beside the review editor)."""
    if draft.failure_report is not None:
        show_failure_report(draft.failure_report)
    if not draft.rounds:
        st.info("아직 모의 응시 결과가 없습니다.")
        return
    latest = draft.rounds[-1]
    st.markdown(f"#### 모의 응시 점수표 ({latest.number}회차)")
    st.dataframe(score_frame(latest), hide_index=True, width="stretch", key=f"{key}-scores")
    st.markdown(verdict_line(latest))
    st.markdown("#### 회차 이력")
    tabs = st.tabs([f"{r.number}회차" for r in draft.rounds])
    for tab, rnd in zip(tabs, draft.rounds, strict=True):
        with tab:
            show_round(rnd, key=f"{key}-r{rnd.number}")


def show_round(rnd: DraftRound, *, key: str) -> None:
    st.markdown(f"**바뀐 구성요소**: {', '.join(c.ko for c in rnd.changed_components) or '없음'}")
    st.markdown(f"**바뀐 이유**: {rnd.change_reason}")
    st.markdown(f"**판정**: {verdict_line(rnd)}")
    st.dataframe(score_frame(rnd), hide_index=True, width="stretch", key=f"{key}-scores")
    by_level = {r.persona_level: r for r in rnd.mock_exam_results}
    for level in LEVELS:
        if level in by_level:
            show_submission(by_level[level], key=f"{key}-{level.value}")


def show_submission(result: MockExamResult, *, key: str) -> None:
    label = (f"{result.persona_level.ko} 수준 페르소나 · 평균 {round_(result.mean_score)}점 · "
             f"대화 {sum(t.speaker == 'examinee' for t in result.transcript)}회")
    with st.expander(label):
        st.markdown("**AI 도구 대화 기록**")
        for turn in result.transcript:
            who = "응시자" if turn.speaker == "examinee" else "AI 도구"
            with st.chat_message("user" if turn.speaker == "examinee" else "assistant"):
                st.caption(who)
                st.write(turn.content)
        st.markdown("**최종 산출물**")
        st.write(result.output)
        st.markdown("**채점 근거**")
        rows = [{"채점": f"{n}회", "항목": s.item_index + 1, "점수": s.score, "근거": s.rationale}
                for n, g in enumerate(result.gradings, start=1) for s in g.item_scores]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", key=f"{key}-gr")
