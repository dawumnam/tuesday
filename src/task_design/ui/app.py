"""과제 설계 에이전트 화면 (`streamlit run src/task_design/ui/app.py`).

Screens: 새 초안 (입력 폼) · 초안 목록 · 초안 보기 (상황 후보 선택 / 진행 / 검토 /
결과, chosen by the draft's state) · 대시보드. The 담당자 is picked at the top of the
sidebar (no login). Every action goes through ``TaskDesignService`` (see
``task_design.ui.runtime`` for how it is built; ``TD_FAKE_GATEWAY=1`` = 데모 모드).

``?draft=<id>&staff=<id>`` in the URL reopens a draft after the tab was closed.
"""

from __future__ import annotations

import streamlit as st

from task_design.ui import heartbeat, runtime
from task_design.ui.screens import dashboard, drafts, input_form
from task_design.ui.screens.common import (
    PAGE_DASHBOARD,
    PAGE_DRAFT,
    PAGE_LIST,
    PAGE_NEW,
    PAGES,
)

st.set_page_config(page_title="과제 설계 에이전트", page_icon="📝", layout="wide")

try:
    rt = runtime.get()
except runtime.StartupError as e:
    st.title("과제 설계 에이전트")
    st.error(str(e))
    st.stop()
service = rt.service
ss = st.session_state

# --- 담당자 · 화면 상태 --------------------------------------------------------------

staff = {s.id: s.name for s in service.list_staff()}
if "page" not in ss:  # first run of this session: restore from the URL
    qp = st.query_params
    try:
        wanted = int(qp.get("staff", ""))
    except ValueError:
        wanted = None
    ss["staff_id"] = wanted if wanted in staff else next(iter(staff))
    ss["draft_id"] = qp.get("draft")
    ss["page"] = PAGE_DRAFT if ss["draft_id"] else PAGE_LIST
    ss["ledger"] = heartbeat.HeartbeatLedger()
if "_goto" in ss:
    page, draft_id = ss.pop("_goto")
    ss["page"] = page
    if draft_id:
        ss["draft_id"] = draft_id

with st.sidebar:
    st.title("과제 설계 에이전트")
    staff_id = st.selectbox("담당자", list(staff), format_func=lambda s: staff[s], key="staff_id",
                            help="로그인 없이 나를 고릅니다. 자신이 작성한 초안은 최종 검토할 수 없습니다.")
    page = st.radio("화면", PAGES, key="page", label_visibility="collapsed")
    if rt.demo:
        st.caption("🧪 데모 모드 (TD_FAKE_GATEWAY=1): 모델 호출 없이 예시 응답을 씁니다.")

st.query_params["staff"] = str(staff_id)
if page == PAGE_DRAFT and ss.get("draft_id"):
    st.query_params["draft"] = ss["draft_id"]
elif "draft" in st.query_params:
    del st.query_params["draft"]

# --- 화면 ------------------------------------------------------------------------

ledger: heartbeat.HeartbeatLedger = ss["ledger"]
context = None


def _started(draft_id) -> None:
    ledger.credit_pending(service, draft_id, staff_id=staff_id)


if page == PAGE_NEW:
    input_form.render(service, staff_id=staff_id, demo=rt.demo, on_started=_started)
elif page == PAGE_LIST:
    drafts.render_list(service, staff_id=staff_id, staff=staff)
elif page == PAGE_DRAFT:
    shown = drafts.render_draft(service, ss.get("draft_id"), staff_id=staff_id, staff=staff,
                                stepper=rt.stepper)
    context = shown.id if shown is not None else None
elif page == PAGE_DASHBOARD:
    dashboard.render(service)


# 사람 시간: measured on the screens where a person works on a draft.
@st.fragment
def _heartbeat() -> None:
    heartbeat.render(service, ledger=ledger, staff_id=staff_id, draft_id=context)


if page == PAGE_NEW or context is not None:
    _heartbeat()
