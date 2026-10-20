"""Smoke test of the 데모 모드 단계별 진행 controls (AppTest).

The service runs drafts on background threads with a ``Stepper`` installed, as
the demo runtime does.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from task_design.domain import DraftState, Level, Stage
from task_design.service import TaskDesignService
from task_design.stepper import StepMode, Stepper
from task_design.testing.workflow import (
    Personas,
    install_verification,
    make_input,
    seed_similar_tasks,
)
from task_design.ui import runtime

APP = Path(__file__).resolve().parents[1] / "src" / "task_design" / "ui" / "app.py"
TIMEOUT = 20.0


def texts(at: AppTest) -> str:
    parts = [e.value for e in (*at.markdown, *at.caption, *at.error, *at.warning, *at.info,
                               *at.success, *at.header, *at.subheader)]
    parts += [e.label for e in at.expander]
    return "\n".join(str(p) for p in parts)


def labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def run(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, at.exception
    return at


def wait_for_state(service, draft_id, state: DraftState):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        draft = service.get_draft(draft_id)
        assert draft.error is None, draft.error
        if draft.state is state:
            return draft
        time.sleep(0.01)
    raise AssertionError(f"draft never reached {state}")


@pytest.fixture
def stepped(db, fake_gateway, settings, background):
    personas_started, release = threading.Event(), threading.Event()

    def hold(level: Level, call) -> None:
        personas_started.set()
        assert release.wait(TIMEOUT)

    script = install_verification(fake_gateway, personas=Personas(before_call=hold))
    seed_similar_tasks(db, fake_gateway, settings)
    stepper = Stepper(default_mode=StepMode.STEP)
    service = TaskDesignService(
        db, fake_gateway, settings, observer=stepper,
        run_graph=background,
    )
    runtime.use_service(service, demo=True, stepper=stepper)
    yield service, stepper, script, personas_started, release
    release.set()
    runtime.use_service(None)


def test_step_by_step_controls_walk_a_draft_to_review(stepped):
    service, stepper, script, personas_started, release = stepped
    draft = service.start(make_input(), author_id=1)
    stepper.wait_until_paused(draft.id, timeout=TIMEOUT)

    at = AppTest.from_file(str(APP), default_timeout=30)
    at.query_params["staff"] = "2"
    at.query_params["draft"] = str(draft.id)
    run(at)
    assert at.radio(key=f"step-mode-{draft.id}").value == "단계별 진행"
    assert "다음 단계: **유사 과제 검색**" in texts(at)

    button(at, "다음 단계").click()
    run(at)
    assert stepper.wait_until_paused(draft.id, timeout=TIMEOUT).stage is Stage.GENERATING_CANDIDATES
    run(at)
    shown = texts(at)
    assert "다음 단계: **상황 후보 생성**" in shown
    for section in ("키워드(트라이그램) 순위", "벡터 순위", "RRF 결합", "LLM 리랭크"):
        assert section in shown

    button(at, "다음 단계").click()
    run(at)
    draft = wait_for_state(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
    run(at)
    assert "상황 후보 선택" in texts(at)
    assert any("중복 검사" in e.label for e in at.expander)
    assert any("유사 과제 검색 과정" in e.label for e in at.expander)

    at.button(key=f"pick-{draft.candidates[0].id}").click()
    run(at)
    assert stepper.wait_until_paused(draft.id, timeout=TIMEOUT).stage is Stage.WRITING_INSTRUCTION
    run(at)
    button(at, "다음 단계").click()
    run(at)
    assert stepper.wait_until_paused(draft.id, timeout=TIMEOUT).stage is Stage.WRITING_ATTACHMENTS
    run(at)
    shown = texts(at)
    assert "방금 끝난 단계: **지시문 작성**" in shown
    assert script.task_writer.instructions[0] in shown

    # 자동 진행 → 일시정지 mid 모의 응시 → stops before 채점 → 계속
    at.radio(key=f"step-mode-{draft.id}").set_value("자동 진행")
    run(at)
    assert personas_started.wait(TIMEOUT)
    run(at)
    button(at, "일시정지").click()
    run(at)
    assert stepper.status(draft.id).paused
    release.set()
    assert stepper.wait_until_paused(draft.id, timeout=TIMEOUT).stage is Stage.GRADING
    run(at)
    assert "계속" in labels(at)
    button(at, "계속").click()
    run(at)
    wait_for_state(service, draft.id, DraftState.AWAITING_REVIEW)
