"""`task-design batch`: several input combinations run end to end, left for human review."""

from __future__ import annotations

import json
import re
from uuid import UUID

from task_design.batch import load_combinations
from task_design.cli import DEFAULT_SEED_DIR, EXAMPLE_BATCH_FILE, main
from task_design.domain import DraftState
from task_design.gateway import Role
from task_design.service import TaskDesignService
from task_design.testing.workflow import ensure_competencies, install_verification

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

COMBINATIONS = [
    {
        "competency_ids": ["C1"],
        "audience": {"job": "재무·회계", "career_level": "3년차"},
        "difficulty": "medium",
    },
    {
        "competency_ids": ["C1"],
        "audience": {"job": "영업 관리", "career_level": "신입"},
        "difficulty": "low",
        "memo": "CSV 첨부를 꼭 포함",
    },
]


def write_combinations(tmp_path, combinations=COMBINATIONS):
    path = tmp_path / "combinations.json"
    path.write_text(json.dumps(combinations, ensure_ascii=False), encoding="utf-8")
    return path


def drafts_in_output(out: str) -> list[UUID]:
    return [UUID(m) for m in dict.fromkeys(UUID_RE.findall(out))]


def test_batch_leaves_one_unlabelled_draft_per_combination_awaiting_review(
    tmp_path, capsys, db, fake_gateway, settings
):
    ensure_competencies(db)
    install_verification(fake_gateway, competency_ids=["C1"])

    code = main(["batch", str(write_combinations(tmp_path))], settings=settings,
                gateway=fake_gateway)

    assert code == 0
    ids = drafts_in_output(capsys.readouterr().out)
    assert len(ids) == 2
    service = TaskDesignService(db, fake_gateway, settings)
    drafts = [service.get_draft(i) for i in ids]
    assert [d.state for d in drafts] == [DraftState.AWAITING_REVIEW] * 2
    # ADR 0002: 검토 라벨 are human-only, so a batch never reviews its drafts.
    assert [d.review for d in drafts] == [None, None]
    assert [d.input.audience.job for d in drafts] == ["재무·회계", "영업 관리"]


def test_batch_can_pick_another_scenario_candidate(tmp_path, capsys, db, fake_gateway, settings):
    ensure_competencies(db)
    install_verification(fake_gateway, competency_ids=["C1"])

    code = main(["batch", str(write_combinations(tmp_path, COMBINATIONS[:1])), "--candidate", "3"],
                settings=settings, gateway=fake_gateway)

    assert code == 0
    [draft_id] = drafts_in_output(capsys.readouterr().out)
    draft = TaskDesignService(db, fake_gateway, settings).get_draft(draft_id)
    assert draft.selected_candidate_id == draft.candidates[2].id


def test_a_failing_combination_is_reported_and_the_rest_still_run(
    tmp_path, capsys, db, fake_gateway, settings
):
    ensure_competencies(db)
    script = install_verification(fake_gateway, competency_ids=["C1"])
    first_draft = []

    def personas_failing_the_first_draft(call):
        first_draft[:] = first_draft or [call.draft_id]
        if call.draft_id == first_draft[0]:
            raise RuntimeError("모델 호출 실패")
        return script.personas(call)

    fake_gateway.respond_with(Role.EXAMINEE, personas_failing_the_first_draft)

    code = main(["batch", str(write_combinations(tmp_path))], settings=settings,
                gateway=fake_gateway)

    assert code == 1
    ids = drafts_in_output(capsys.readouterr().out)
    service = TaskDesignService(db, fake_gateway, settings)
    failed, passed = (service.get_draft(i) for i in ids)
    assert failed.error is not None
    assert passed.state is DraftState.AWAITING_REVIEW


def test_the_shipped_example_has_10_to_15_combinations_over_seed_competencies():
    inputs = load_combinations(EXAMPLE_BATCH_FILE)

    assert 10 <= len(inputs) <= 15
    seed_ids = {c["id"] for c in json.loads((DEFAULT_SEED_DIR / "competencies.json").read_text())}
    assert {cid for i in inputs for cid in i.competency_ids} <= seed_ids
    assert len({i.model_dump_json() for i in inputs}) == len(inputs)
