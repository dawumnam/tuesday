-- One row per model call made through the gateway (spec: 비용).
CREATE TABLE model_costs (
    id                  bigserial PRIMARY KEY,
    draft_id            uuid,
    role                text        NOT NULL,
    provider            text        NOT NULL,
    model               text        NOT NULL,
    input_tokens        integer     NOT NULL,
    output_tokens       integer     NOT NULL,
    estimated_cost_usd  numeric(14, 8) NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX model_costs_draft_id_idx ON model_costs (draft_id);
CREATE INDEX model_costs_created_at_idx ON model_costs (created_at);
