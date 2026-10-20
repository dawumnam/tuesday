"""Streamlit entry point (`streamlit run src/task_design/ui/app.py`).

Placeholder from ticket 11 so the compose app service has something to serve;
ticket 10 replaces it with the five screens. When building the real service here,
construct the gateway with ``build_gateway(settings, usage_sink=CostStore(db))``,
otherwise costs are not recorded and the token budget never stops the fix loop.
"""

from __future__ import annotations

import streamlit as st

from task_design.db import Database
from task_design.settings import Settings

st.set_page_config(page_title="과제 설계 에이전트")
st.title("과제 설계 에이전트")
st.info("화면은 준비 중입니다. 지금은 `task-design batch`로 초안을 만들 수 있습니다.")

db = Database(Settings().database_url)
try:
    with db.connection() as conn:
        staff = conn.execute("SELECT name FROM staff ORDER BY id").fetchall()
        n_tasks = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
finally:
    db.close()
st.write(f"담당자: {', '.join(name for (name,) in staff)} · 과제 DB: {n_tasks}개")
