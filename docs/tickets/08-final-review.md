---
status: closed
blocked-by: [06]
closed-by: db0363a
---
# 08 Final review

Spec: `review` operation; 데이터 구조 → 검토, 담당자. User stories 34–41; ADR 0002.

- `review(draft, reviewer, labels, edited_version, decision)`: only from 검토 대기; reviewer ≠ author; labels required for all of 상황/지시문/채점 기준 (유지 / 부분 수정 / 전면 재작성); edited version validated with domain rules.
- 확정 → insert task (with edits) into the task DB via ticket 03's repository so it appears in hybrid search. 반려 → draft kept with labels.
- Tests via service: author reviewing own draft rejected; missing label rejected; confirm makes the task findable by `hybrid_search`; reject preserves labels and state 반려. Get drafts into 검토 대기 using the ticket 06 flow with FakeGateway.
