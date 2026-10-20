"""Workflow part 2 through the service: select_scenario → generation → mock exam → verdict."""

from __future__ import annotations

import uuid

import pytest

from task_design.service import UnknownCompetency
from task_design.testing.workflow import make_input


def test_start_rejects_competencies_not_in_the_framework(service, fake_gateway):
    with pytest.raises(UnknownCompetency):
        service.start(make_input(competency_ids=[f"NO-{uuid.uuid4().hex[:6]}"]), author_id=1)
    assert fake_gateway.calls == []
