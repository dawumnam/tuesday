"""데모 모드 단계별 진행: controls on the 진행 screen and what each stage produced.

Only shown when the runtime installed a ``Stepper`` (``TD_FAKE_GATEWAY=1``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pandas as pd
import streamlit as st

from task_design.domain import (
    Attachment,
    ChatTurn,
    Draft,
    Grading,
    Rubric,
    ScenarioCandidate,
    Verdict,
)
from task_design.stepper import StepMode, Stepper
from task_design.traces import DuplicateCheckTrace, SearchTrace
from task_design.ui.screens.common import (
    LEVELS,
    rubric_items_frame,
    show_attachment_list,
    show_transcript,
    verdict_text,
)
from task_design.workflow.observer import StepPoint

_MODES = {m.ko: m for m in StepMode}
_FINISH_LABEL = "검토 대기로 넘기기"


def _label(point: StepPoint) -> str:
    return point.stage.ko if point.stage is not None else _FINISH_LABEL


def controls(stepper: Stepper, draft: Draft) -> None:
    """진행 방식 choice, 다음 단계 / 일시정지 / 계속, and the last stage's result."""
    status = stepper.status(draft.id)
    key = f"step-mode-{draft.id}"
    st.session_state[key] = status.mode.ko  # follow the stepper (another tab may change it)

    def changed() -> None:
        stepper.set_mode(draft.id, _MODES[st.session_state[key]])

    with st.container(border=True):
        left, right = st.columns([3, 2], vertical_alignment="center")
        left.radio("진행 방식 (데모)", list(_MODES), key=key, horizontal=True, on_change=changed,
                   help="단계별 진행: 단계마다 멈추고 '다음 단계'로 한 단계씩 실행합니다. "
                        "자동 진행: 끝까지 실행하며 '일시정지'로 다음 단계 앞에서 멈출 수 있습니다.")
        with right:
            # on_click: the stepper changes before the rerun, so the screen shows it at once.
            if status.waiting is not None:
                st.button("다음 단계", key=f"step-next-{draft.id}", type="primary",
                          on_click=stepper.step, args=(draft.id,))
            if status.mode is StepMode.AUTO:
                if status.paused:
                    st.button("계속", key=f"step-resume-{draft.id}",
                              on_click=stepper.resume, args=(draft.id,))
                else:
                    st.button("일시정지", key=f"step-pause-{draft.id}",
                              on_click=stepper.pause, args=(draft.id,))

        if status.waiting is not None:
            st.markdown(f"⏸️ 다음 단계: **{_label(status.waiting)}**")
            if status.previous is None:
                st.caption("아직 실행한 단계가 없습니다. '다음 단계'를 누르면 이 단계만 실행합니다.")
            else:
                st.markdown(f"방금 끝난 단계: **{_label(status.previous)}**")
                _show_output(stepper, draft, status.previous, status.waiting.state)
        elif status.paused:
            st.caption("일시정지했습니다. 지금 단계가 끝나면 다음 단계 앞에서 멈춥니다.")
        elif status.mode is StepMode.STEP:
            st.caption("이번 단계를 실행하는 중입니다. 끝나면 다음 단계 앞에서 멈춥니다.")


# --- 방금 끝난 단계의 결과 ------------------------------------------------------------


def _show_output(stepper: Stepper, draft: Draft, previous: StepPoint,
                 state: Mapping[str, Any]) -> None:
    match previous.node:
        case "search_similar_tasks":
            trace = stepper.search_trace(draft.id)
            if trace is not None:
                show_search_trace(trace)
        case "generate_candidates":
            chosen = _chosen_candidate(state)
            if chosen is not None:
                st.markdown(f"**고른 상황 후보: {chosen.title}**")
                st.write(chosen.scenario)
            else:
                st.caption("이전 후보와 겹치지 않는 상황 후보를 다시 만듭니다.")
        case "write_instruction":
            st.markdown("**지시문**")
            st.write(state.get("instruction", ""))
        case "write_attachments":
            st.markdown("**첨부 자료**")
            show_attachment_list([Attachment.model_validate(a)
                                  for a in state.get("attachments", [])])
        case "write_rubric":
            if state.get("rubric"):
                st.markdown("**채점 기준**")
                st.dataframe(rubric_items_frame(Rubric.model_validate(state["rubric"])),
                             hide_index=True, width="stretch")
        case "review_task":
            _show_review(state)
        case "start_mock_exam":
            _show_submissions(state)
        case "start_grading":
            _show_gradings(state)
        case "judge":
            if state.get("verdict"):
                st.markdown(f"**판정**: {verdict_text(Verdict.model_validate(state['verdict']))}")
        case "fix":
            changes = state.get("round_changes") or {}
            st.markdown(f"**자기 수정 → {state.get('round', '?')}회차**: {changes.get('reason', '')}")


def _chosen_candidate(state: Mapping[str, Any]) -> ScenarioCandidate | None:
    if state.get("selection") != "select":
        return None
    for c in state.get("candidates", []):
        if str(c["id"]) == state.get("selected_candidate_id"):
            return ScenarioCandidate.model_validate(c)
    return None


def _show_review(state: Mapping[str, Any]) -> None:
    findings = state.get("review_findings") or {}
    if findings.get("consistent", True):
        st.markdown("**검토 에이전트**: 지시문과 채점 기준이 서로 맞습니다.")
    else:
        part = "지시문" if findings.get("target") == "instruction" else "채점 기준"
        st.markdown(f"**검토 에이전트**: 문제를 찾아 {part}을(를) 한 번 다시 만들었습니다.")
        for issue in findings.get("issues", []):
            st.markdown(f"- {issue}")
    st.caption("상황·지시문·첨부 자료·채점 기준을 과제로 묶었습니다. 아래 '현재 초안 미리 보기'에서 볼 수 있습니다.")


def _round_entries(state: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    rnd = state.get("round")
    return [e for e in state.get(key, []) if e.get("round") == rnd]


def _show_submissions(state: Mapping[str, Any]) -> None:
    subs = {s["level"]: s for s in _round_entries(state, "submissions")}
    st.markdown("**응시자 페르소나 제출물**")
    for level in LEVELS:
        sub = subs.get(level.value)
        if sub is None:
            continue
        transcript = [ChatTurn.model_validate(t) for t in sub["transcript"]]
        asks = sum(t.speaker == "examinee" for t in transcript)
        with st.expander(f"{level.ko} 수준 페르소나 · AI 도구 대화 {asks}회"):
            show_transcript(transcript)
            st.markdown("**최종 산출물**")
            st.write(sub["output"])


def _show_gradings(state: Mapping[str, Any]) -> None:
    totals: dict[str, list[float]] = {}
    for g in sorted(_round_entries(state, "gradings"), key=lambda g: g["index"]):
        totals.setdefault(g["level"], []).append(Grading.model_validate(g["grading"]).total)
    rows = [{"응시자 페르소나": lv.ko,
             **{f"채점 {i + 1}": t for i, t in enumerate(totals.get(lv.value, []))}}
            for lv in LEVELS]
    st.markdown("**채점 결과 (제출물마다 3회)**")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


# --- 유사 과제 검색 · 중복 검사 과정 ----------------------------------------------------


def _short(text: str, n: int = 60) -> str:
    return text if len(text) <= n else text[:n] + "…"


def _table(rows: list[dict[str, Any]]) -> None:
    if not rows:
        st.caption("해당 과제가 없습니다.")
        return
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 height=min(38 + 35 * len(rows), 260))


def show_search_trace(trace: SearchTrace) -> None:
    st.markdown("##### ① 키워드(트라이그램) 순위")
    st.caption("검색어: " + " · ".join(f"‘{t}’" for t in trace.terms))
    st.caption(f"검색어마다 따로 상황+지시문+직무와의 단어 유사도(pg_trgm)를 재고, "
               f"{trace.keyword_min_word_similarity} 이상이면 일치로 칩니다. 일치한 검색어가 "
               f"많은 순, 같으면 유사도 합이 큰 순이며, 하나도 일치하지 않으면 순위에 들지 않습니다.")
    _table([{"순위": h.keyword_rank,
             "일치한 검색어": ", ".join(f"{m.term} ({m.similarity:.2f})" for m in h.keyword_matches),
             "일치 수": len(h.keyword_matches),
             "유사도 합": round(h.keyword_score or 0, 3),
             "상황": _short(h.scenario)}
            for h in sorted((h for h in trace.hits if h.keyword_rank),
                            key=lambda h: h.keyword_rank or 0)])
    st.markdown("##### ② 벡터 순위")
    st.caption("상황 임베딩의 코사인 유사도 순입니다.")
    _table([{"순위": h.semantic_rank, "코사인 유사도": round(h.cosine or 0, 3),
             "상황": _short(h.scenario)}
            for h in sorted((h for h in trace.hits if h.semantic_rank),
                            key=lambda h: h.semantic_rank or 0)])
    st.markdown("##### ③ RRF 결합")
    st.caption(f"점수 = Σ 1/({trace.rrf_k} + 순위) (두 순위 중 있는 것만 더함). "
               f"상위 {trace.rerank_pool}개가 리랭크로 넘어갑니다.")
    _table([{"RRF 순위": h.fused_rank, "키워드 순위": h.keyword_rank,
             "벡터 순위": h.semantic_rank, "RRF 점수": round(h.fused_score, 5),
             "상황": _short(h.scenario)} for h in trace.hits])
    st.markdown("##### ④ LLM 리랭크")
    st.caption(f"리랭커가 다시 매긴 순서의 상위 {trace.k}개를 상황 후보 생성에 유사 과제로 넘깁니다.")
    _table([{"최종 순위": h.final_rank, "RRF 순위": h.fused_rank, "상황": _short(h.scenario)}
            for h in trace.final()])


_OUTCOME = {
    "accepted": "✅ 통과",
    "db_duplicate": "❌ 기존 과제와 중복",
    "peer_duplicate": "❌ 다른 후보와 중복",
    "already_shown": "❌ 이미 보여 준 후보",
}


def show_duplicate_trace(trace: DuplicateCheckTrace) -> None:
    rejected = len(trace.proposals) - len(trace.accepted())
    st.caption(f"후보마다 과제 DB에서 가장 비슷한 과제와의 코사인 유사도를 재고, {trace.threshold} 이상이면 "
               f"버리고 다시 생성합니다 (이번 배치: 제안 {len(trace.proposals)}개, 탈락 {rejected}개).")
    _table([{
        "생성": f"{p.attempt}회차",
        "후보": _short(p.title, 30),
        "DB 최대 유사도": None if p.max_db_similarity is None else round(p.max_db_similarity, 3),
        "가장 비슷한 과제": _short(p.nearest_task_scenario or "", 40),
        "후보 간 유사도": None if p.max_peer_similarity is None
        else round(p.max_peer_similarity, 3),
        "결과": _OUTCOME[p.outcome],
    } for p in trace.proposals])


def show_traces(stepper: Stepper, draft: Draft) -> None:
    """상황 후보 선택 screen: the 유사 과제 검색 and 중복 검사 behind these candidates."""
    search, dups = stepper.search_trace(draft.id), stepper.duplicate_trace(draft.id)
    if search is not None:
        with st.expander("🔎 유사 과제 검색 과정 (키워드 · 벡터 · RRF · 리랭크)"):
            show_search_trace(search)
    if dups is not None:
        with st.expander("🧪 상황 후보 중복 검사"):
            show_duplicate_trace(dups)


