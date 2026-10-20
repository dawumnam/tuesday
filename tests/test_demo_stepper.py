"""데모 단계별 진행 through the service: a ``Stepper`` installed as the run observer.

The service runs graph work on a background thread here (as in the app), so a
run paused at a stage boundary does not block the test.
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

from task_design.db import Database
from task_design.domain import DraftState, Level, Stage, Verdict
from task_design.gateway import Role
from task_design.repositories import KEYWORD_MIN_WORD_SIMILARITY, RRF_K, TaskRepository
from task_design.service import TaskDesignService
from task_design.stepper import StepMode, Stepper, StepPoint
from task_design.testing.workflow import (
    Personas,
    ScenarioWriter,
    by_schema,
    ensure_competencies,
    install_verification,
    make_input,
    make_task,
    random_unit,
    scripted_batch,
    seed_similar_tasks,
    start_awaiting_review,
    with_cosine,
)
from task_design.workflow.schemas import CandidateBatch

TIMEOUT = 20.0


def stepped_service(
    db, fake, settings, background, mode: StepMode
) -> tuple[TaskDesignService, Stepper]:
    stepper = Stepper(default_mode=mode)
    service = TaskDesignService(db, fake, settings, run_graph=background, observer=stepper)
    return service, stepper


def wait_for_state(service, draft_id, state: DraftState):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        draft = service.get_draft(draft_id)
        assert draft.error is None, draft.error
        if draft.state is state:
            return draft
        time.sleep(0.01)
    raise AssertionError(f"draft never reached {state}")


def step_through(service, stepper: Stepper, draft_id, until: DraftState) -> list[StepPoint]:
    """Press 다음 단계 at every pause until the draft reaches ``until``; the pauses seen."""
    seen: list[StepPoint] = []
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        draft = service.get_draft(draft_id)
        assert draft.error is None, draft.error
        if draft.state is until:
            return seen
        waiting = stepper.status(draft_id).waiting
        if waiting is not None:
            seen.append(waiting)
            stepper.step(draft_id)
        time.sleep(0.01)
    raise AssertionError(f"draft never reached {until}; paused at {[p.node for p in seen]}")


def open_transactions(db: Database) -> int:
    with db.connection() as conn:
        return conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%%'"
        ).fetchone()[0]


def test_step_mode_pauses_before_every_stage_and_next_releases_exactly_one(
    db, fake_gateway, settings, background
):
    service, stepper = stepped_service(db, fake_gateway, settings, background, StepMode.STEP)
    script = install_verification(fake_gateway)
    seed_similar_tasks(db, fake_gateway, settings)
    calls_before = len(fake_gateway.calls)

    draft = service.start(make_input(), author_id=1)

    point = stepper.wait_until_paused(draft.id, timeout=TIMEOUT)
    assert point.stage is Stage.SEARCHING_SIMILAR_TASKS
    time.sleep(0.3)  # nothing runs while paused
    assert stepper.status(draft.id).waiting == point
    assert service.get_draft(draft.id).similar_tasks == []
    assert len(fake_gateway.calls) == calls_before
    assert open_transactions(db) == 0

    stepper.step(draft.id)
    point = stepper.wait_until_paused(draft.id, timeout=TIMEOUT)
    assert point.stage is Stage.GENERATING_CANDIDATES
    assert stepper.status(draft.id).previous.stage is Stage.SEARCHING_SIMILAR_TASKS
    paused = service.get_draft(draft.id)
    assert paused.similar_tasks and paused.candidates == []
    assert not any(c.role is Role.GENERATION for c in fake_gateway.calls)

    stepper.step(draft.id)
    draft = wait_for_state(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
    service.select_scenario(draft.id, draft.candidates[0].id)

    seen = step_through(service, stepper, draft.id, DraftState.AWAITING_REVIEW)

    # The parallel 모의 응시 and 채점 fan-outs are one stage each; 마무리 has no stage.
    assert [p.stage for p in seen] == [
        Stage.WRITING_INSTRUCTION, Stage.WRITING_ATTACHMENTS, Stage.WRITING_RUBRIC,
        Stage.REVIEWING, Stage.MOCK_EXAM, Stage.GRADING, Stage.JUDGING, None,
    ]
    # Each pause carries what the stages before it produced.
    by_stage = {p.stage: p.state for p in seen}
    assert by_stage[Stage.WRITING_ATTACHMENTS]["instruction"] == script.task_writer.instructions[0]
    assert "attachments" not in by_stage[Stage.WRITING_ATTACHMENTS]
    assert by_stage[Stage.WRITING_RUBRIC]["attachments"]
    assert by_stage[Stage.REVIEWING]["rubric"]
    assert len(by_stage[Stage.GRADING]["submissions"]) == 3
    assert not by_stage[Stage.GRADING].get("gradings")
    assert len(by_stage[Stage.JUDGING]["gradings"]) == 9
    assert Verdict.model_validate(by_stage[None]["verdict"]).failures == []


def test_pause_in_auto_mode_stops_at_the_next_stage_boundary_and_resume_continues(
    db, fake_gateway, settings, background
):
    service, stepper = stepped_service(db, fake_gateway, settings, background, StepMode.AUTO)
    personas_started, release = threading.Event(), threading.Event()

    def hold(level: Level, call) -> None:
        personas_started.set()
        assert release.wait(TIMEOUT)

    install_verification(fake_gateway, personas=Personas(before_call=hold))
    seed_similar_tasks(db, fake_gateway, settings)
    draft = service.start(make_input(), author_id=1)
    draft = wait_for_state(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
    service.select_scenario(draft.id, draft.candidates[0].id)

    assert personas_started.wait(TIMEOUT)
    stepper.pause(draft.id)  # mid 모의 응시
    release.set()

    point = stepper.wait_until_paused(draft.id, timeout=TIMEOUT)
    assert point.stage is Stage.GRADING
    time.sleep(0.3)
    assert not any(c.role is Role.GRADING for c in fake_gateway.calls)
    assert service.get_draft(draft.id).state is DraftState.VERIFYING
    assert open_transactions(db) == 0

    stepper.resume(draft.id)
    wait_for_state(service, draft.id, DraftState.AWAITING_REVIEW)


def test_switching_a_paused_draft_to_auto_runs_it_to_the_end(
    db, fake_gateway, settings, background
):
    service, stepper = stepped_service(db, fake_gateway, settings, background, StepMode.STEP)
    install_verification(fake_gateway)
    seed_similar_tasks(db, fake_gateway, settings)
    draft = service.start(make_input(), author_id=1)
    stepper.wait_until_paused(draft.id, timeout=TIMEOUT)

    stepper.set_mode(draft.id, StepMode.AUTO)

    wait_for_state(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
    assert stepper.status(draft.id).mode is StepMode.AUTO


def test_auto_mode_observer_leaves_an_inline_run_unchanged(db, fake_gateway, settings):
    service = TaskDesignService(db, fake_gateway, settings,
                                observer=Stepper(default_mode=StepMode.AUTO))

    draft = start_awaiting_review(service, fake_gateway, db, settings)

    assert draft.state is DraftState.AWAITING_REVIEW
    assert draft.error is None


# --- 유사 과제 검색 · 중복 검사 traces --------------------------------------------------


def test_search_trace_explains_how_the_similar_tasks_were_found(
    db, fake_gateway, settings, background
):
    service, stepper = stepped_service(db, fake_gateway, settings, background, StepMode.STEP)
    seed_similar_tasks(db, fake_gateway, settings)
    fake_gateway.respond_with(Role.GENERATION, by_schema({CandidateBatch: ScenarioWriter()}))
    draft = service.start(make_input(), author_id=1)

    stepper.wait_until_paused(draft.id, timeout=TIMEOUT)
    assert stepper.search_trace(draft.id) is None  # 유사 과제 검색 has not run yet
    stepper.step(draft.id)
    stepper.wait_until_paused(draft.id, timeout=TIMEOUT)  # before 상황 후보 생성
    trace = stepper.search_trace(draft.id)
    paused = service.get_draft(draft.id)

    assert trace is not None and trace.rrf_k == RRF_K
    assert [h.task_id for h in trace.final()] == [t.id for t in paused.similar_tasks]
    assert len(trace.final()) == min(settings.similar_tasks_k, len(trace.hits))
    for h in trace.hits:
        assert h.keyword_rank is not None or h.semantic_rank is not None
        if h.word_similarity is not None:
            assert h.word_similarity >= KEYWORD_MIN_WORD_SIMILARITY
        expected = sum(1 / (RRF_K + r) for r in (h.keyword_rank, h.semantic_rank) if r)
        assert h.fused_score == pytest.approx(expected)
    assert [h.fused_rank for h in trace.hits] == list(range(1, len(trace.hits) + 1))
    assert all(a.fused_score >= b.fused_score for a, b in zip(trace.hits, trace.hits[1:]))
    semantic = sorted((h for h in trace.hits if h.semantic_rank), key=lambda h: h.semantic_rank)
    assert all(a.cosine >= b.cosine for a, b in zip(semantic, semantic[1:]))

    stepper.step(draft.id)
    draft = wait_for_state(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)
    final_ids = {h.task_id for h in trace.final()}
    for c in draft.candidates:
        assert set(c.referenced_task_ids) <= final_ids


def test_duplicate_check_trace_shows_rejected_and_accepted_candidates(
    db, fake_gateway, settings, background
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
    service, stepper = stepped_service(db, fake_gateway, settings, background, StepMode.AUTO)

    draft = service.start(make_input(), author_id=1)
    draft = wait_for_state(service, draft.id, DraftState.AWAITING_SCENARIO_SELECTION)

    trace = stepper.duplicate_trace(draft.id)
    assert trace is not None and trace.threshold == settings.duplicate_threshold
    [rejected] = [p for p in trace.proposals if p.outcome != "accepted"]
    assert rejected.scenario == near_copy and rejected.outcome == "db_duplicate"
    assert rejected.attempt == 1
    assert rejected.max_peer_similarity is None  # no candidate accepted before it
    assert rejected.max_db_similarity == pytest.approx(0.95, abs=1e-3)
    assert rejected.nearest_task_scenario == existing
    assert [p.scenario for p in trace.accepted()] == [c.scenario for c in draft.candidates]
    assert trace.accepted()[-1].attempt == 2  # the replacement came from a second call
    for p in trace.accepted():
        assert p.max_db_similarity < settings.duplicate_threshold
