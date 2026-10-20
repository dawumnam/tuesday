-- 생성·검증 results on the 초안 (ticket 06). get_draft reads these.
ALTER TABLE drafts
    ADD COLUMN task           jsonb,                        -- Task (current content)
    ADD COLUMN rounds         jsonb NOT NULL DEFAULT '[]',  -- list[DraftRound], ordered by number
    ADD COLUMN persona_status jsonb NOT NULL DEFAULT '{}',  -- {Level value: PersonaStatus value}
    ADD COLUMN failure_report jsonb;                        -- FailureReport (ticket 07 writes it)
