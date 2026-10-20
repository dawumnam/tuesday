---
status: open
blocked-by: [01]
---
# 02 Model gateway

Spec: 모듈 → 모델 게이트웨이; 모델 라우팅 table; Further Notes (model IDs); ADR 0003. User stories 46, 47. (53, 54 → ticket 12.)

Implement the real `ModelGateway` behind ticket 01's protocol.

- Provider: Gemini only (google-genai SDK; completions with thinking level, JSON-schema structured output, embeddings). **All roles default to Gemini 3.8 Flash, thinking high** (embedding → Gemini embedding model). The local LLM provider is deferred to ticket 12 — keep the provider abstraction so it slots in, but don't build it here.
- Routing from settings per role (provider + model + thinking + budget), so ticket 12 only adds a provider and flips config.
- Every output validated against the requested pydantic schema; on parse/validation failure retry (configurable, default 2) with the error fed back.
- Per call: record tokens in/out, call count, estimated cost (price table in settings) against the draft id → persisted in a `model_costs` table (migration). Expose a read function the metrics ticket can use.
- Langfuse: one trace per draft run, one span/generation per call (prompt, response, latency, tokens). No-op when keys are absent.
- **Look up and record** the real API model IDs for "Gemini 3.8 Flash" and the Gemini embedding model — write findings to `docs/research/model-ids.md` and put the IDs as settings defaults. If something can't be confirmed, say so in that file.
- Tests (service-boundary style where possible, otherwise gateway-level with the HTTP layer stubbed): role routing follows settings; schema retry; cost accumulation per draft. No real network in tests.
