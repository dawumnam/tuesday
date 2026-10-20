#!/bin/sh
# App container start: migrate, optionally seed, then serve the Streamlit UI.
set -eu

python -m task_design.migrate

case "${TD_SEED_ON_START:-false}" in
  1|true|TRUE|yes)
    # Idempotent; embeds the 30 seed tasks with the real gateway (needs TD_GEMINI_API_KEY).
    # A failed seed (e.g. no API key) is logged but does not keep the UI from starting.
    task-design seed || echo "경고: 시드 적재에 실패했습니다. API 키를 확인한 뒤 'task-design seed'를 다시 실행하세요." >&2
    ;;
esac

exec streamlit run src/task_design/ui/app.py \
  --server.address=0.0.0.0 --server.port=8501 --server.headless=true \
  --browser.gatherUsageStats=false
