---
status: closed
blocked-by: []
closed-by: d2192e6
---
# 01 Foundation

Spec sections: Implementation Decisions → 모듈, 데이터 구조, 실행 환경; Testing Decisions → 환경.

Build the skeleton every later ticket plugs into. Later tickets run **in parallel** against this, so the seams below are the contract.

## Conventions (binding for all tickets)

- Python 3.13, `uv`, package `task_design` under `src/`. Tests in `tests/`, `pytest`.
- Code identifiers in English; all task content (prompts, seed data, generated text) in Korean. Add a "Code names" section to `GLOSSARY.md` mapping each term to its identifier (e.g. 초안 → `Draft`, 상황 후보 → `ScenarioCandidate`, 채점 기준 → `Rubric`, 과정 항목 → `RubricItemType.PROCESS`, 검토 라벨 → `ReviewLabel`).
- `psycopg` 3 + plain SQL migrations in `migrations/NNNN_name.sql`, applied by a tiny runner in order (tracked in a `schema_migrations` table). Each later ticket adds its own numbered migration; pick the next free number at merge time.
- Domain types: pydantic v2 models in `task_design/domain.py` (draft states, task, attachment with trap markers, rubric item with competency id / type / points / 0·partial·full criteria and the "≥1 process item, 3–5 items" validation, draft round, mock exam result, review, measurement event, cost).
- Settings: `pydantic-settings` in `task_design/settings.py` — DB URL, verdict thresholds (solvable ≥70, gap ≥30, grading stdev ≤5), duplicate threshold 0.85, max rounds 3, per-draft token budget, role→provider/model/thinking/token-budget map, `use_local_llm` switch, local endpoint URL, Langfuse keys. All overridable via env.
- Staff: exactly 2 fixed 담당자 seeded by migration.

## Deliverables

- `docker-compose.yml`: Postgres with pgvector (+ pg_trgm), Langfuse (with its own Postgres). App service can be stubbed here; ticket 11 finishes it.
- **Model gateway seam**: `task_design/gateway.py` with a `ModelGateway` protocol — role enum (generation, review, examinee, ai_tool, grading, rerank, embedding); `complete(role, messages, schema, draft_id) -> validated object`; `embed(texts) -> list[vector]`; `rerank(query, docs) -> order`. Provide `FakeGateway` in `task_design/testing/fake_gateway.py` that is scripted per role (queue of canned responses / callables) and records calls and token usage. Ticket 02 builds the real one.
- **Service seam**: `task_design/service.py` `TaskDesignService` constructed with (db, gateway, settings) exposing every operation from the spec with `NotImplementedError` bodies — later tickets fill them.
- Test harness: `tests/conftest.py` fixture that connects to Postgres from `TEST_DATABASE_URL` (default the compose instance), **creates a fresh uniquely-named database per test session**, applies migrations, and drops it after — multiple worktrees will run tests concurrently against the same server. A `service` fixture wiring a `FakeGateway`.
- One passing test proving: migrations apply, both extensions exist, the 2 staff exist, `TaskDesignService` constructs.
- `Makefile` or `justfile` targets: `up`, `test`, `migrate`.
