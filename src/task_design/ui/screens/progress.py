"""진행: live stage + per-persona status while 생성 중 / 검증 중 (polls ``get_draft``).

The run itself is in the server's background executor, so this screen can be
closed and reopened (초안 목록 or ``?draft=<id>``) without stopping it.
"""

from __future__ import annotations

import streamlit as st

from task_design.domain import Draft, DraftState, PersonaStatus, Stage
from task_design.stepper import Stepper
from task_design.ui.screens import stepping
from task_design.ui.screens.common import LEVELS, show_task, verdict_line
from task_design.workflow.observer import StepPoint

POLL_SECONDS = 2.0

_SCENARIO_STAGES = [Stage.SEARCHING_SIMILAR_TASKS, Stage.GENERATING_CANDIDATES]
_VERIFY_STAGES = [Stage.WRITING_INSTRUCTION, Stage.WRITING_ATTACHMENTS, Stage.WRITING_RUBRIC,
                  Stage.REVIEWING, Stage.MOCK_EXAM, Stage.GRADING, Stage.JUDGING]
_PERSONA_ICON = {PersonaStatus.PENDING: "⏸️", PersonaStatus.RUNNING: "✍️",
                 PersonaStatus.DONE: "✅"}


def render(service, draft: Draft, *, stepper: Stepper | None = None) -> None:
    st.caption("이 화면을 닫아도 실행은 서버에서 계속됩니다. 나중에 '초안 목록'에서 다시 열 수 있습니다.")
    _live(service, draft.id, draft.state, draft.error, stepper)


@st.fragment(run_every=POLL_SECONDS)
def _live(service, draft_id, shown_state: DraftState, shown_error: str | None,
          stepper: Stepper | None) -> None:
    draft = service.get_draft(draft_id)
    if draft.state is not shown_state or draft.error != shown_error:
        st.rerun(scope="app")  # another screen takes over (후보 선택, 검토, 오류)
    held = None
    if stepper is not None:  # 데모 모드: 단계별 진행 / 자동 진행
        held = stepper.status(draft.id).waiting
        stepping.controls(stepper, draft)
    show_progress(draft, held=held)


def show_progress(draft: Draft, *, held: StepPoint | None = None) -> None:
    """``held``: the stage boundary a 데모 모드 단계별 진행 run waits at (marked ⏸️)."""
    if draft.state is DraftState.GENERATING:
        st.subheader("상황 후보를 만드는 중입니다")
        _steps(_SCENARIO_STAGES, draft.stage, held)
        return
    finishing = held is not None and held.stage is None  # 판정 done, before 검토 대기
    number = len(draft.rounds) + (0 if finishing and draft.rounds else 1)
    st.subheader(f"{number}회차 검증 완료" if finishing else f"{number}회차 검증 중")
    if held is not None:
        stage_text = f"{held.stage.ko if held.stage else '검토 대기로 넘기기'} 앞에서 멈춤"
    else:
        stage_text = draft.stage.ko if draft.stage else "다음 단계 준비 중"
    st.markdown(f"현재 단계: **{stage_text}**")
    stages = _VERIFY_STAGES if number == 1 else [Stage.FIXING, *_VERIFY_STAGES[4:]]
    if number > 1 and draft.stage not in stages and draft.stage is not None:
        stages = [draft.stage, *stages]
    _steps(stages, draft.stage, held)

    st.markdown("#### 응시자 페르소나")
    cols = st.columns(3)
    for col, level in zip(cols, LEVELS, strict=True):
        status = draft.persona_status.get(level)
        text = status.ko if status else "대기"
        col.metric(f"{level.ko} 수준", f"{_PERSONA_ICON.get(status, '⏸️')} {text}")
    st.caption("세 페르소나는 같은 AI 도구 LLM으로 동시에 응시하고, 제출물마다 3회 채점합니다.")

    if draft.rounds:
        st.markdown("#### 끝난 회차")
        for rnd in draft.rounds:
            st.markdown(f"- {rnd.number}회차: {verdict_line(rnd)}")
            if rnd.number > 1:
                st.caption(rnd.change_reason)
    if draft.task is not None:
        with st.expander("현재 초안 미리 보기"):
            show_task(draft.task, key=f"pv-{draft.id}")


def _steps(stages: list[Stage], current: Stage | None, held: StepPoint | None = None) -> None:
    idx = stages.index(current) if current in stages else -1
    marker = "▶"
    if held is not None:  # the stage it waits before has not started yet
        marker = "⏸️"
        idx = stages.index(held.stage) if held.stage in stages else len(stages)
    lines = []
    for i, stage in enumerate(stages):
        if i < idx:
            lines.append(f"✓ {stage.ko}")
        elif i == idx:
            lines.append(f"**{marker} {stage.ko}**")
        else:
            lines.append(f"○ {stage.ko}")
    st.markdown("  \n".join(lines))
