.PHONY: up infra db down migrate seed batch report eval-search test

# Everything: app (Streamlit, http://localhost:8501) + Postgres + Langfuse.
up:
	docker compose up -d --build --wait

# Postgres + Langfuse only (run the app or CLI from the host).
infra:
	docker compose up -d --wait db langfuse-db langfuse

# Only the app Postgres (enough for tests).
db:
	docker compose up -d --wait db

down:
	docker compose down

migrate:
	uv run python -m task_design.migrate

seed:
	uv run task-design seed

batch:
	uv run task-design batch examples/batch.json

# 성공 기준 실측 (last 30 days) as a Markdown table.
report:
	uv run task-design report

# 유사 과제 검색 적중률 over the seed query set (real embeddings + rerank).
eval-search:
	uv run task-design eval-search examples/search_eval.json

test: db
	uv run pytest
