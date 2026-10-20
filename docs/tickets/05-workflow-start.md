---
status: closed
blocked-by: [03]
closed-by: 3810816
---
# 05 Workflow part 1 — start, regenerate, get_draft

Spec: 모듈 → 과제 설계 서비스 (`start`, `regenerate_scenarios`, `get_draft`), 워크플로우; 초안 상태. User stories 1–9, 16, 31, 33.

- LangGraph state graph with `langgraph-checkpoint-postgres` checkpointer (thread id = draft id). Nodes for this ticket: similar-task search → scenario candidate generation (3, mutually distinct) + duplicate check (candidates over threshold are regenerated, bounded) → `interrupt` for human selection.
- Draft persistence table(s): draft id, author, input (1–2 competencies, job, career level, difficulty, optional memo), state, candidates with their referenced similar tasks, current stage (for progress display). States: `생성 중` → `상황 선택 대기`.
- `start(input, author)`, `regenerate_scenarios(draft)` (only valid in 상황 선택 대기), `get_draft(draft)` returning state, stage, candidates, rounds, mock-exam results (empty for now).
- Leave the graph open for ticket 06 to add nodes after the interrupt — keep node registration easy to extend.
- Tests via the service only: start → 3 candidates each listing referenced tasks, state 상황 선택 대기; a duplicate candidate is regenerated; regenerate returns 3 new candidates; input validation (competency count 1–2); a new service instance (simulated restart) can still `get_draft` and later resume.
