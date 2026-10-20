.PHONY: up db down migrate test

# Everything: app Postgres + Langfuse.
up:
	docker compose up -d --wait db langfuse-db langfuse

# Only the app Postgres (enough for tests).
db:
	docker compose up -d --wait db

down:
	docker compose down

migrate:
	uv run python -m task_design.migrate

test: db
	uv run pytest
