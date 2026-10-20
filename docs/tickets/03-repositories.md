---
status: closed
closed-by: 30d30d9a82c1ca64bf78f18041ab158cba643aeb
blocked-by: [01]
---
# 03 Task & competency repositories

Spec: 모듈 → 과제 저장소, 역량 체계 저장소; 데이터 구조 → 과제. User stories 6, 7, 40, 48–50.

- Migration: competency framework tables (competency → sub-element → level indicators 상/중/하), tasks table (content fields from spec + `embedding vector(N)` dimension from settings + status/source), trigram index on searchable text, vector index.
- `CompetencyRepository`: read-only lookups (by id, list, level indicators for a competency+level).
- `TaskRepository`: insert confirmed task (computes embedding via gateway), `hybrid_search(query_text, k)` = pg_trgm similarity ranking + vector cosine ranking fused by RRF, then top-N reranked via `gateway.rerank`; `find_duplicates(text, threshold)` by embedding cosine (threshold from settings, default 0.85).
- Loader that accepts the same structure as real data (story 50): JSON files in a documented schema → tables. Ticket 04 provides the synthetic files.
- Tests (real Postgres, FakeGateway with controlled vectors): keyword-only match is found, semantic-only match is found, rerank order applied, duplicate threshold respected.
