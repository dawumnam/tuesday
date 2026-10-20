---
status: closed
blocked-by: [05]
closed-by: d7735f1
---
# 06 Workflow part 2 — select_scenario happy path

Spec: 워크플로우 node order; 모의 응시 (ADR 0001); 판정 thresholds. User stories 10–22, 32.

- `select_scenario(draft, candidate)` resumes the interrupt; state `검증 중` while running, `검토 대기` when done.
- Nodes: instruction → attachments (text files with marked traps) → rubric (domain-validated, 3–5 items, ≥1 process) → review agent (instruction/rubric consistency; its findings feed one regeneration of the offending part before the mock exam) → mock exam: 상·중·하 examinee personas **in parallel**, same model, level indicators injected, behaviours per spec, persona ↔ ai_tool conversation ≤6 turns, submission = transcript + final output → grading: 3 independent gradings per submission with per-item scores and rationale → verdict (solvable, discrimination, grading consistency from settings thresholds).
- Persist round 1 with mock exam results and verdict; `get_draft` exposes them plus the current stage (story 31/32: which personas are running).
- This ticket ends the graph at verdict → 검토 대기 regardless of pass/fail; ticket 07 adds the fix loop at that edge.
- Tests via service with FakeGateway scripts: full happy path ends 검토 대기 with 3 persona results, 3 gradings each, all checks passing; verdict correctly reports each failure type given scripted scores (no loop yet); resume from checkpoint after a simulated crash mid-run gives the same result.
