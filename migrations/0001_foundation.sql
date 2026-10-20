-- Foundation: extensions and the two fixed 담당자 (staff).
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE staff (
    id   integer PRIMARY KEY,
    name text NOT NULL UNIQUE
);

INSERT INTO staff (id, name) VALUES
    (1, '김지현'),
    (2, '박서준');
