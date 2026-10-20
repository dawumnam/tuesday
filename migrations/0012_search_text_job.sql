-- Keyword search (유사 과제 검색) also matches the task's 대상자 직무, so a 검색어 term
-- such as "마케터" finds tasks written for that job even when the 상황 doesn't name it.
-- A generated column's expression can't be altered: drop and re-add (index included).
DROP INDEX tasks_search_text_trgm_idx;
ALTER TABLE tasks DROP COLUMN search_text;
ALTER TABLE tasks ADD COLUMN search_text text GENERATED ALWAYS AS (
    scenario || E'\n' || instruction || E'\n' || coalesce(audience ->> 'job', '')
) STORED;
CREATE INDEX tasks_search_text_trgm_idx ON tasks USING gist (search_text gist_trgm_ops);
