---
status: closed
blocked-by: [03]
closed-by: 94c370b
---
# 04 Synthetic seed data

Spec: User stories 48–50; Out of Scope (real data).

- Hand-author (as committed JSON in `seed/`, Korean) a synthetic AI-literacy competency framework: ~8 competencies, sub-elements, 상·중·하 level indicators written as observable AI-usage behaviours (ADR 0001).
- ~30 synthetic tasks with 상황, 지시문, text attachments (CSV/메모/이메일/보고서) with trap markers, and rubrics satisfying the domain validation (3–5 items, ≥1 process item), spread across competencies, job roles, career levels and difficulties.
- CLI command `task-design seed` (load via ticket 03's loader; embeddings via the configured gateway). Idempotent.
- Test: seeding into a fresh DB with FakeGateway yields the expected counts and every seeded rubric passes validation; a hybrid search over seeded data returns results.
