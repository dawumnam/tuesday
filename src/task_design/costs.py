"""Per-call model usage persisted to ``model_costs`` (spec: 비용).

``CostStore`` is the ``UsageSink`` the real gateway reports to, and the read
side the metrics ticket aggregates from.
"""

from __future__ import annotations

from uuid import UUID

from task_design.db import Database
from task_design.domain import Cost, Interval
from task_design.gateway import Usage

_AGGREGATE = """
    SELECT draft_id, role, sum(input_tokens), sum(output_tokens), count(*),
           sum(estimated_cost_usd)
    FROM model_costs
    WHERE {where}
    GROUP BY draft_id, role
    ORDER BY draft_id, role
"""


class CostStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, usage: Usage) -> None:
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO model_costs
                    (draft_id, role, provider, model, input_tokens, output_tokens,
                     estimated_cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    usage.draft_id,
                    usage.role.value,
                    usage.provider,
                    usage.model,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.estimated_cost_usd,
                ),
            )

    def for_draft(self, draft_id: UUID) -> list[Cost]:
        """One ``Cost`` per role used by the draft, ordered by role."""
        return self._aggregate("draft_id = %s", (draft_id,))

    def for_drafts(self, draft_ids: list[UUID]) -> list[Cost]:
        """Per draft and role, for all calls of the given drafts."""
        if not draft_ids:
            return []
        return self._aggregate("draft_id = ANY(%s)", (list(draft_ids),))

    def for_period(self, period: Interval) -> list[Cost]:
        """Per draft and role, for calls made within ``period`` (start inclusive,
        end exclusive). Calls without a draft id are left out."""
        return self._aggregate(
            "draft_id IS NOT NULL AND created_at >= %s AND created_at < %s",
            (period.start, period.end),
        )

    def _aggregate(self, where: str, params: tuple) -> list[Cost]:
        with self.db.connection() as conn:
            rows = conn.execute(_AGGREGATE.format(where=where), params).fetchall()
        return [
            Cost(
                draft_id=draft_id,
                role=role,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                calls=calls,
                estimated_cost_usd=float(cost),
            )
            for draft_id, role, input_tokens, output_tokens, calls, cost in rows
        ]
