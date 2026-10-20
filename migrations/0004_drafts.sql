-- 초안 persistence. The workflow writes here; get_draft reads only this table
-- (the LangGraph checkpoint tables are for resuming runs, not for reads).
CREATE TABLE drafts (
    id                    uuid PRIMARY KEY,
    author_id             integer NOT NULL REFERENCES staff (id),
    input                 jsonb NOT NULL,   -- DraftInput
    state                 text NOT NULL,    -- DraftState value
    stage                 text,             -- Stage value while a step runs
    similar_tasks         jsonb NOT NULL DEFAULT '[]',  -- list[Task] (with ids)
    candidates            jsonb NOT NULL DEFAULT '[]',  -- list[ScenarioCandidate]
    selected_candidate_id uuid,
    error                 text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX drafts_author_created_idx ON drafts (author_id, created_at DESC);
