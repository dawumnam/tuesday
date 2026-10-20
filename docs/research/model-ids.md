# Gemini model IDs (ticket 02)

Checked 2026-10-20 against Google's Gemini API docs (ai.google.dev). Settings defaults live in
`src/task_design/settings.py` (`DEFAULT_GEMINI_MODEL`, `DEFAULT_GEMINI_EMBEDDING_MODEL`, `DEFAULT_PRICES`).

## Decisions

| Role | Model ID (setting default) | Status |
|---|---|---|
| generation, review, examinee, ai_tool, grading, rerank | `gemini-3.8-flash`, thinking `high` | Confirmed |
| embedding | `gemini-embedding-2`, `output_dimensionality=768` | Confirmed, with one conflicting doc page (see below) |

## Gemini 3.8 Flash: `gemini-3.8-flash`

- The models page lists "Gemini 3.8 Flash: `gemini-3.8-flash`" under **Gemini 3: Stable**. The model's own page also says Stable. Source: https://ai.google.dev/gemini-api/docs/models and https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash
- Limits: 1,048,576 input tokens and 65,536 output tokens. It supports structured outputs and thinking.
- Thinking levels are `low`, `medium` and `high`, and the API default is `medium`. **`minimal` is not supported and returns an error.** Our `ThinkingLevel.NONE` therefore sends no thinking config, which means the API default (medium) applies. It does not turn thinking off. Source: https://ai.google.dev/gemini-api/docs/thinking
- Pricing on the paid tier, per 1M tokens: input $0.75 and output $3.75 (including thinking tokens) **through 2026-12-31**, then **$1.50 / $7.50 from 2027-01-01**. The default price table uses the 2026 rates. From January, override them with `TD_PRICES='{"gemini-3.8-flash": {"input_usd_per_mtok": 1.5, "output_usd_per_mtok": 7.5}, ...}'` (this replaces the whole dict, so include the embedding entry too). Source: https://ai.google.dev/gemini-api/docs/pricing
- Thinking tokens are billed as output. The gateway records `output_tokens = candidatesTokenCount + thoughtsTokenCount`.

## Embedding: `gemini-embedding-2`

- The embeddings guide lists both `gemini-embedding-2` and `gemini-embedding-001` as "Stable". Source: https://ai.google.dev/gemini-api/docs/embeddings
- **Conflict:** the models overview lists the model as `gemini-embedding-2-preview`, while the embeddings guide and the pricing page use `gemini-embedding-2`. We use `gemini-embedding-2`. If the API rejects it, set `TD_ROUTING__EMBEDDING__MODEL=gemini-embedding-2-preview`. This could not be checked without a real API call.
- Both models default to 3072 dimensions, accept 128 to 3072, and Google recommends 768, 1536 or 3072. We keep `embedding_dim=768`.
- `gemini-embedding-2` auto-normalises truncated dimensions, but `gemini-embedding-001` does not. The gateway L2-normalises every vector anyway, so switching models is safe.
- `gemini-embedding-2` does not accept `task_type`; you put a task prefix in the text instead. We send neither.
- **SDK pitfall (google-genai 2.31):** for model names that contain `gemini-embedding-2`, `embed_content(contents=[str, str])` folds the strings into one multi-part Content and returns **one** aggregated vector. The gateway therefore sends one `Content` per text (covered by `test_embeddings_use_the_embedding_model_at_the_configured_dimension`).
- Pricing: text input costs $0.20 per 1M tokens. There is no output price. The pricing page no longer lists `gemini-embedding-001`.
- The Gemini API returns no token usage for embeddings, so the gateway estimates input tokens as `ceil(len(text) / 3)`.

## API surface

- The current Gemini docs teach the newer **Interactions API** (`client.interactions.create`). `models.generateContent` is still documented in the API reference with no deprecation notice, and the SDK still supports it. The gateway uses `generate_content` with `system_instruction`, `thinking_config.thinking_level`, `response_mime_type="application/json"` and `response_json_schema=<pydantic schema>`. If generateContent is deprecated later, only `GeminiProvider` in `src/task_design/providers.py` needs to change.

## Not verified

- No real API call was made, so the IDs above come from the docs only. Before the first real run, try one call per model (`gemini-3.8-flash` with thinking high, and `gemini-embedding-2` at 768 dims).
- The Qwen local model ID is out of scope here (ticket 12).
