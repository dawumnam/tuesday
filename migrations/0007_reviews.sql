-- 최종 검토 (ticket 08). One review per draft (확정 and 반려 are final states).
-- Ticket 09 reads these by period: filter reviews.reviewed_at, join review_labels.
CREATE TABLE reviews (
    draft_id    uuid PRIMARY KEY REFERENCES drafts (id) ON DELETE CASCADE,
    reviewer_id integer NOT NULL REFERENCES staff (id),
    decision    text NOT NULL CHECK (decision IN ('confirm', 'reject')),  -- ReviewDecision
    edited_task jsonb,  -- Task as the reviewer submitted it (NULL: no edits)
    task_id     uuid,   -- 확정 과제 id in tasks (NULL for 반려)
    reviewed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX reviews_reviewed_at_idx ON reviews (reviewed_at);

-- 검토 라벨: one row per labeled component (상황, 지시문, 채점 기준).
CREATE TABLE review_labels (
    draft_id  uuid NOT NULL REFERENCES reviews (draft_id) ON DELETE CASCADE,
    component text NOT NULL,  -- Component value
    label     text NOT NULL CHECK (label IN ('keep', 'partial_edit', 'full_rewrite')),  -- ReviewLabel
    PRIMARY KEY (draft_id, component)
);
