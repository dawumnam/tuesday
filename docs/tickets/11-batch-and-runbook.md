---
status: open
blocked-by: [02, 04, 07]
---
# 11 Batch generation, app container, runbook

Spec: User stories 51–52; 실행 환경; Testing Decisions → 수동 품질 검증.

- CLI `task-design batch <file>`: run 10–15 input combinations end-to-end (auto-pick candidate 1, or a strategy flag), leaving drafts in 검토 대기 for human labelling (ADR 0002 — never auto-label). Ship an example combinations file.
- Finish the compose app service so `docker compose up` starts app + DB + Langfuse, runs migrations, and optionally seeds.
- README: setup, env vars, Gemini API key, demo runbook. (Local LLM docs belong to ticket 12.)
- Test: batch command with FakeGateway over 2 combinations produces 2 drafts in 검토 대기.
