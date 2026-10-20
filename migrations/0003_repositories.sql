-- 역량 체계 (competency → sub-element → 상·중·하 level indicators) and 과제 DB.

CREATE TABLE competencies (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT ''
);

CREATE TABLE competency_sub_elements (
    id            text PRIMARY KEY,
    competency_id text NOT NULL REFERENCES competencies (id) ON DELETE CASCADE,
    position      integer NOT NULL,
    name          text NOT NULL,
    description   text NOT NULL DEFAULT ''
);
CREATE INDEX competency_sub_elements_competency_idx
    ON competency_sub_elements (competency_id, position);

CREATE TABLE level_indicators (
    sub_element_id text NOT NULL REFERENCES competency_sub_elements (id) ON DELETE CASCADE,
    level          text NOT NULL CHECK (level IN ('high', 'medium', 'low')),
    description    text NOT NULL,
    PRIMARY KEY (sub_element_id, level)
);

-- 과제 DB. The embedding dimension must equal Settings.embedding_dim (768);
-- Postgres rejects vectors of any other length on insert.
CREATE TABLE tasks (
    id             uuid PRIMARY KEY,
    scenario       text NOT NULL,
    instruction    text NOT NULL,
    attachments    jsonb NOT NULL DEFAULT '[]',
    rubric         jsonb NOT NULL,
    competency_ids text[] NOT NULL,
    audience       jsonb NOT NULL,
    difficulty     text NOT NULL CHECK (difficulty IN ('high', 'medium', 'low')),
    embedding      vector(768) NOT NULL,
    source         text NOT NULL CHECK (source IN ('seed', 'agent')),
    status         text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
    search_text    text GENERATED ALWAYS AS (scenario || E'\n' || instruction) STORED,
    created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX tasks_search_text_trgm_idx ON tasks USING gist (search_text gist_trgm_ops);
CREATE INDEX tasks_embedding_hnsw_idx ON tasks USING hnsw (embedding vector_cosine_ops);
