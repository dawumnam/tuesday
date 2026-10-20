---
status: open
blocked-by: [01]
---
# 02 Model gateway

Spec: 모듈 → 모델 게이트웨이; 모델 라우팅 table; Further Notes (model IDs); ADR 0003. User stories 46, 47, 53, 54.

Implement the real `ModelGateway` behind ticket 01's protocol.

- Providers: Gemini (google-genai SDK; completions with thinking level, JSON-schema structured output, embeddings) and OpenAI-compatible (vLLM on `ml` via SSH tunnel; URL from settings).
- Routing from settings per role. `use_local_llm=false` routes examinee + ai_tool roles to Gemini.
- Every output validated against the requested pydantic schema; on parse/validation failure retry (configurable, default 2) with the error fed back.
- Per call: record tokens in/out, call count, estimated cost (price table in settings) against the draft id → persisted in a `model_costs` table (migration). Expose a read function the metrics ticket can use.
- Langfuse: one trace per draft run, one span/generation per call (prompt, response, latency, tokens). No-op when keys are absent.
- **Look up and record** the real API model IDs for "Gemini 3.8 Flash", the Gemini embedding model and Qwen3.8-27B 4bit (does a 4bit quant exist? license?) — write findings to `docs/research/model-ids.md` and put the IDs as settings defaults. If something can't be confirmed, say so in that file.
- Tests (service-boundary style where possible, otherwise gateway-level with the HTTP layer stubbed): local switch off → examinee/ai_tool go to Gemini provider; schema retry; cost accumulation per draft. No real network in tests.
