-- 측정 이벤트 (ticket 09): 사람 활성 구간 (record_activity) and 에이전트 실행 구간
-- (one per graph execution, written by the service around every run).
-- Overlapping/duplicate intervals are fine: metrics takes the union per draft and kind.
CREATE TABLE measurement_events (
    id         bigserial PRIMARY KEY,
    draft_id   uuid NOT NULL REFERENCES drafts (id) ON DELETE CASCADE,
    staff_id   integer REFERENCES staff (id),  -- NULL for agent_run
    kind       text NOT NULL CHECK (kind IN ('human_active', 'agent_run')),  -- IntervalKind
    started_at timestamptz NOT NULL,
    ended_at   timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ended_at >= started_at),
    CHECK ((kind = 'agent_run') = (staff_id IS NULL))
);

CREATE INDEX measurement_events_draft_idx ON measurement_events (draft_id);
CREATE INDEX drafts_created_at_idx ON drafts (created_at);
