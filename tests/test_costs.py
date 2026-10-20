"""Reading recorded model costs back (the metrics ticket's input)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from task_design.costs import CostStore
from task_design.domain import Interval
from task_design.gateway import Role, Usage
from task_design.testing.fake_gateway import FakeGateway


def usage(role: Role, draft_id, tokens: int, cost: float) -> Usage:
    return Usage(
        role=role,
        draft_id=draft_id,
        provider="gemini",
        model="m",
        input_tokens=tokens,
        output_tokens=tokens,
        estimated_cost_usd=cost,
    )


def test_period_costs_cover_calls_made_inside_the_period(db):
    costs = CostStore(db)
    draft = uuid4()
    before = datetime.now(UTC)
    costs.record(usage(Role.GENERATION, draft, 10, 0.5))
    costs.record(usage(Role.GENERATION, draft, 20, 0.25))
    costs.record(usage(Role.EMBEDDING, None, 99, 9.0))  # not attributable to a draft
    after = datetime.now(UTC) + timedelta(seconds=1)

    inside = [c for c in costs.for_period(Interval(start=before, end=after)) if c.draft_id == draft]
    earlier = costs.for_period(Interval(start=before - timedelta(days=1), end=before))

    assert [(c.role, c.calls, c.input_tokens) for c in inside] == [("generation", 2, 30)]
    assert inside[0].estimated_cost_usd == pytest.approx(0.75)
    assert all(c.draft_id != draft for c in earlier)


def test_fake_gateway_usage_reaches_the_cost_table(db):
    costs = CostStore(db)
    fake = FakeGateway(usage_sink=costs)
    fake.script(Role.EXAMINEE, "답변")
    draft = uuid4()

    fake.complete(Role.EXAMINEE, [], draft_id=draft)

    assert [(c.role, c.calls) for c in costs.for_draft(draft)] == [("examinee", 1)]
