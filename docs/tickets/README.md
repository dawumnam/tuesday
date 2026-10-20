# Tickets — spec 0001 (과제 설계 에이전트 MVP)

Local markdown tracker. Each ticket has a frontmatter block:

- `status`: `open` | `closed` — work is closed by setting `status: closed` and adding `closed-by: <commit sha>`.
- `blocked-by`: tickets that must be closed first.

Spec: `docs/specs/0001-task-design-agent-mvp.md`. Terms: `GLOSSARY.md`. Decisions: `docs/adr/`.

| # | Title | Blocked by |
|---|---|---|
| 01 | Foundation: scaffold, compose, settings, schema, service skeleton, fake gateway | — |
| 02 | Model gateway: Gemini for all roles, schema retry, cost, Langfuse | 01 |
| 03 | Task & competency repositories: hybrid search, duplicate check | 01 |
| 04 | Synthetic seed data command | 03 |
| 05 | Workflow part 1: `start` / `regenerate_scenarios` / `get_draft` with interrupt + Postgres checkpoint | 03 |
| 06 | Workflow part 2: `select_scenario` generation → review agent → mock exam → grading → verdict (happy path) | 05 |
| 07 | Self-correction loop + failure report | 06 |
| 08 | Final review: `review`, confirm into task DB, reject preservation | 06 |
| 09 | Measurement: `record_activity`, agent intervals, `metrics` | 08 |
| 10 | Streamlit screens | 07, 08, 09 |
| 11 | Batch generation command, app container, README / demo runbook | 02, 04, 07 |
| 12 | Local LLM for personas + AI tool — **deferred; hosting (local rig vs ml RTX 6000 Ada) to be re-decided with user** | 02, 11 |
