-- Kind of the last workflow failure (DraftErrorKind value), next to the raw error text.
-- The UI picks its Korean message by kind instead of reading the raw text.
ALTER TABLE drafts ADD COLUMN error_kind text;

UPDATE drafts
SET error_kind = CASE WHEN error LIKE 'Interrupted:%' THEN 'interrupted' ELSE 'unexpected' END
WHERE error IS NOT NULL;
