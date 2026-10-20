---
status: open
blocked-by: [08]
---
# 09 Measurement & metrics

Spec: 모듈 → 측정; `record_activity`, `metrics(기간)`. User stories 42–47.

- `record_activity(draft, staff, active_interval)` stores human-active intervals (UI sends only active, tab-visible intervals). Workflow records agent-run intervals (start/select_scenario/fix rounds) — hook into the existing graph runs.
- `metrics(period)`: pass-without-full-rewrite ratio over reviewed drafts; mean total work time per task (human active + agent run, overlapping intervals not double counted); label distribution per component; per-draft LLM cost and call count (from ticket 02's `model_costs` — if ticket 02 isn't merged, FakeGateway's usage recording must flow to the same table: coordinate via the gateway protocol from ticket 01); link to the Langfuse trace per draft.
- Tests via service: ratio with mixed labels; total time excludes gaps between active intervals; cost sums.
