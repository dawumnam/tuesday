"""Workflow part 1 through the service: start, regenerate_scenarios, get_draft."""

from __future__ import annotations

import math
import random
import uuid

import pytest

from task_design.db import Database
from task_design.domain import DraftErrorKind, DraftState, Stage
from task_design.gateway import Role
from task_design.repositories import TaskRepository
from task_design.service import (
    DraftNotFound,
    InvalidDraftInput,
    InvalidDraftState,
    TaskDesignService,
)
from task_design.testing.workflow import (
    ScenarioWriter,
    ensure_competencies,
    make_input,
    make_task,
    scripted_batch,
    seed_similar_tasks,
    start_awaiting_selection,
)


def random_unit(dim: int) -> list[float]:
    rng = random.Random(uuid.uuid4().int)
    v = [rng.gauss(0, 1) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def with_cosine(v: list[float], cosine: float) -> list[float]:
    """A unit vector whose cosine similarity with unit vector ``v`` is ``cosine``."""
    w = random_unit(len(v))
    dot = sum(a * b for a, b in zip(v, w, strict=True))
    w = [b - dot * a for a, b in zip(v, w, strict=True)]
    n = math.sqrt(sum(x * x for x in w))
    s = math.sqrt(1 - cosine * cosine)
    return [cosine * a + s * b / n for a, b in zip(v, w, strict=True)]


def test_start_offers_three_candidates_each_referencing_similar_tasks(
    service, fake_gateway, db, settings
):
    seed_similar_tasks(db, fake_gateway, settings)
    fake_gateway.respond_with(Role.GENERATION, ScenarioWriter())

    draft = service.start(make_input(), author_id=1)

    assert draft.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert draft.author_id == 1
    assert len(draft.candidates) == 3
    assert len({c.id for c in draft.candidates}) == 3
    assert len({c.scenario for c in draft.candidates}) == 3
    similar_ids = {t.id for t in draft.similar_tasks}
    assert similar_ids
    for candidate in draft.candidates:
        assert candidate.referenced_task_ids
        assert set(candidate.referenced_task_ids) <= similar_ids


def test_candidate_too_similar_to_an_existing_task_is_regenerated(
    service, fake_gateway, db, settings
):
    existing = f"기존 과제 {uuid.uuid4().hex}"
    near_copy = f"거의 같은 상황 {uuid.uuid4().hex}"
    v = random_unit(settings.embedding_dim)
    fake_gateway.set_embedding(existing, v)
    fake_gateway.set_embedding(near_copy, with_cosine(v, 0.95))
    ensure_competencies(db)
    TaskRepository(db, fake_gateway, settings).add(make_task(existing), source="seed")
    writer = ScenarioWriter()
    fake_gateway.script(Role.GENERATION, scripted_batch(near_copy, "후보 A " + uuid.uuid4().hex,
                                                        "후보 B " + uuid.uuid4().hex))
    fake_gateway.respond_with(Role.GENERATION, writer)

    draft = service.start(make_input(), author_id=1)

    scenarios = [c.scenario for c in draft.candidates]
    assert len(scenarios) == 3
    assert near_copy not in scenarios
    assert writer.scenarios[0] in scenarios  # the replacement


def test_candidates_too_similar_to_each_other_are_regenerated(
    service, fake_gateway, db, settings
):
    first = f"후보 원본 {uuid.uuid4().hex}"
    twin = f"후보 쌍둥이 {uuid.uuid4().hex}"
    v = random_unit(settings.embedding_dim)
    fake_gateway.set_embedding(first, v)
    fake_gateway.set_embedding(twin, with_cosine(v, 0.9))
    ensure_competencies(db)
    writer = ScenarioWriter()
    fake_gateway.script(Role.GENERATION, scripted_batch(first, twin, "후보 C " + uuid.uuid4().hex))
    fake_gateway.respond_with(Role.GENERATION, writer)

    draft = service.start(make_input(), author_id=1)

    scenarios = [c.scenario for c in draft.candidates]
    assert len(scenarios) == 3
    assert first in scenarios and twin not in scenarios


def test_a_start_that_cannot_find_three_non_duplicate_candidates_fails_and_can_be_retried(
    service, fake_gateway, db, settings
):
    existing = f"기존 과제 {uuid.uuid4().hex}"
    v = random_unit(settings.embedding_dim)
    fake_gateway.set_embedding(existing, v)
    ensure_competencies(db)
    TaskRepository(db, fake_gateway, settings).add(make_task(existing), source="seed")

    def near_copies(call):
        texts = [f"거의 같은 상황 {uuid.uuid4().hex}" for _ in range(3)]
        for t in texts:
            fake_gateway.set_embedding(t, with_cosine(v, 0.95))
        return scripted_batch(*texts)(call)

    fake_gateway.respond_with(Role.GENERATION, near_copies)

    stuck = service.start(make_input(), author_id=1)

    assert stuck.state is DraftState.GENERATING
    assert stuck.error_kind is DraftErrorKind.CANDIDATES_EXHAUSTED
    assert stuck.candidates == []

    fake_gateway.respond_with(Role.GENERATION, ScenarioWriter())
    retried = service.retry(stuck.id)
    assert retried.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert retried.error is None and retried.error_kind is None
    assert len(retried.candidates) == 3


def test_regeneration_never_offers_a_scenario_that_was_already_shown(
    service, fake_gateway, db, settings
):
    draft = start_awaiting_selection(service, fake_gateway, db, settings)
    shown = [c.scenario for c in draft.candidates]
    fake_gateway.respond_with(Role.GENERATION, scripted_batch(*shown))

    stuck = service.regenerate_scenarios(draft.id)

    assert stuck.error_kind is DraftErrorKind.CANDIDATES_EXHAUSTED
    assert stuck.state is DraftState.GENERATING


def test_regeneration_skips_already_shown_scenarios_and_keeps_looking(
    service, fake_gateway, db, settings
):
    draft = start_awaiting_selection(service, fake_gateway, db, settings)
    shown = [c.scenario for c in draft.candidates]
    writer = ScenarioWriter()
    fake_gateway.script(Role.GENERATION, scripted_batch(shown[0], shown[1], "새 후보 " + uuid.uuid4().hex))
    fake_gateway.respond_with(Role.GENERATION, writer)

    again = service.regenerate_scenarios(draft.id)

    offered = [c.scenario for c in again.candidates]
    assert len(offered) == 3
    assert not set(shown) & set(offered)


def test_regenerate_replaces_candidates_with_three_new_ones(service, fake_gateway, db, settings):
    draft = start_awaiting_selection(service, fake_gateway, db, settings)
    before = {c.scenario for c in draft.candidates}

    again = service.regenerate_scenarios(draft.id)

    assert again.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert len(again.candidates) == 3
    assert not before & {c.scenario for c in again.candidates}
    assert not {c.id for c in draft.candidates} & {c.id for c in again.candidates}
    assert all(c.referenced_task_ids for c in again.candidates)


def test_failed_generation_leaves_draft_generating_with_error_and_blocks_regenerate(
    service, fake_gateway, db, settings
):
    def llm_down(call):
        raise RuntimeError("LLM down")

    seed_similar_tasks(db, fake_gateway, settings)
    fake_gateway.script(Role.GENERATION, llm_down)

    stuck = service.start(make_input(), author_id=1)

    assert stuck.state is DraftState.GENERATING
    assert "LLM down" in stuck.error
    assert service.get_draft(stuck.id).error == stuck.error
    with pytest.raises(InvalidDraftState):
        service.regenerate_scenarios(stuck.id)


@pytest.mark.parametrize("competency_ids", [[], ["C1", "C2", "C3"], ["C1", "C1"]])
def test_start_requires_one_or_two_distinct_competencies(service, fake_gateway, competency_ids):
    raw = make_input().model_dump(mode="json") | {"competency_ids": competency_ids}

    with pytest.raises(InvalidDraftInput):
        service.start(raw, author_id=1)
    assert fake_gateway.calls == []


@pytest.mark.parametrize("audience", [{"job": "", "career_level": "3년차"},
                                      {"job": "마케팅", "career_level": "  "}])
def test_start_requires_a_job_and_career_level(service, fake_gateway, audience):
    raw = make_input().model_dump(mode="json") | {"audience": audience}

    with pytest.raises(InvalidDraftInput):
        service.start(raw, author_id=1)
    assert fake_gateway.calls == []


def test_two_competencies_are_accepted(service, fake_gateway, db, settings):
    draft = start_awaiting_selection(
        service, fake_gateway, db, settings, input=make_input(competency_ids=["C1", "C2"])
    )

    assert draft.input.competency_ids == ["C1", "C2"]
    assert draft.state is DraftState.AWAITING_SCENARIO_SELECTION


def test_draft_survives_a_restart_and_can_be_resumed(fake_gateway, db, settings):
    draft = start_awaiting_selection(
        TaskDesignService(db, fake_gateway, settings), fake_gateway, db, settings
    )

    restarted_db = Database(settings.database_url)
    try:
        restarted = TaskDesignService(restarted_db, fake_gateway, settings)
        seen = restarted.get_draft(draft.id)
        assert seen.state is DraftState.AWAITING_SCENARIO_SELECTION
        assert seen.candidates == draft.candidates
        assert seen.similar_tasks == draft.similar_tasks

        again = restarted.regenerate_scenarios(draft.id)
    finally:
        restarted_db.close()

    assert again.state is DraftState.AWAITING_SCENARIO_SELECTION
    assert len(again.candidates) == 3
    assert not {c.scenario for c in draft.candidates} & {c.scenario for c in again.candidates}


def test_get_draft_reports_stage_while_running_and_clears_it_when_waiting(
    service, fake_gateway, db, settings
):
    stages_seen = []
    writer = ScenarioWriter()

    def writer_that_peeks(call):
        stages_seen.append(service.get_draft(call.draft_id).stage)
        return writer(call)

    fake_gateway.respond_with(Role.GENERATION, writer_that_peeks)
    draft = start_awaiting_selection(service, fake_gateway, db, settings)

    assert stages_seen == [Stage.GENERATING_CANDIDATES]
    assert draft.stage is None
    assert draft.rounds == []


def test_get_draft_of_unknown_id_raises(service):
    with pytest.raises(DraftNotFound):
        service.get_draft(uuid.uuid4())
