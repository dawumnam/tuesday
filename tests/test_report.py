"""성공 기준 실측: 수작업 기준선, 자동 검증 vs 사람 검토, and `task-design report`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from task_design.cli import main
from task_design.domain import Component, Interval, Level, ReviewDecision, ReviewLabel
from task_design.measurement import KST, period_of_days
from task_design.report import render_report
from task_design.service import TaskDesignService
from task_design.testing.fake_gateway import FakeGateway
from task_design.testing.workflow import start_awaiting_review

AUTHOR, REVIEWER = 1, 2
H, M, L = Level.HIGH, Level.MEDIUM, Level.LOW

ALL_KEEP = dict.fromkeys(
    (Component.SCENARIO, Component.INSTRUCTION, Component.RUBRIC), ReviewLabel.KEEP
)
REWRITE = {**ALL_KEEP, Component.RUBRIC: ReviewLabel.FULL_REWRITE}


class SteppingClock:
    """Each reading is ``step`` later than the one before."""

    def __init__(self, step: timedelta) -> None:
        self.now, self.step = datetime(2031, 1, 1, tzinfo=UTC) - step, step

    def __call__(self) -> datetime:
        self.now += self.step
        return self.now


def now() -> datetime:
    return datetime.now(UTC)


def bracket(start: datetime) -> Interval:
    return Interval(start=start, end=now() + timedelta(seconds=1))


def review_one(db, settings, labels, decision, *, scores=None, clock=None) -> None:
    """One reviewed draft from its own gateway (so each draft can script its own scores)."""
    fake = FakeGateway(embedding_dim=settings.embedding_dim)
    kwargs = {} if clock is None else {"clock": clock}
    service = TaskDesignService(db=db, gateway=fake, settings=settings, **kwargs)
    extra = {} if scores is None else {"scores": scores}
    draft = start_awaiting_review(service, fake, db, settings, author_id=AUTHOR, **extra)
    service.review(draft.id, REVIEWER, labels, decision)


def test_work_time_is_compared_with_the_manual_baseline(db, settings):
    began = now()
    # Two graph runs (start, select) of 12 minutes each: 24 minutes of agent time.
    review_one(db, settings, ALL_KEEP, ReviewDecision.CONFIRM,
               clock=SteppingClock(timedelta(minutes=12)))
    service = TaskDesignService(db=db, gateway=FakeGateway(), settings=settings)

    m = service.metrics(bracket(began))

    assert m.baseline_work_minutes == 60
    assert m.mean_total_work_minutes == pytest.approx(24)
    assert m.work_minutes_saved_ratio == pytest.approx(0.6)  # 1 − 24/60
    assert m.mean_agent_minutes == pytest.approx(24)
    assert m.mean_human_minutes == pytest.approx(0)


def test_a_period_without_reviews_has_no_saved_share(service):
    m = service.metrics(Interval(start=datetime(2001, 1, 1, tzinfo=UTC),
                                 end=datetime(2001, 2, 1, tzinfo=UTC)))

    assert m.work_minutes_saved_ratio is None
    assert (m.judged_drafts, m.agreeing_drafts) == (0, 0)


def test_agreement_compares_the_automatic_verdict_with_the_human_review(db, settings):
    one_fix = settings.model_copy(update={"max_fix_rounds": 1})
    failing = {H: 60, M: 50, L: 20}  # 풀이 불가: goes to review with a 실패 리포트
    began = now()
    review_one(db, one_fix, ALL_KEEP, ReviewDecision.CONFIRM)  # passed, kept: agree
    review_one(db, one_fix, REWRITE, ReviewDecision.CONFIRM)  # passed, rewritten: disagree
    review_one(db, one_fix, ALL_KEEP, ReviewDecision.REJECT, scores=failing)  # failed, 반려: agree
    review_one(db, one_fix, ALL_KEEP, ReviewDecision.CONFIRM, scores=failing)  # failed, kept
    service = TaskDesignService(db=db, gateway=FakeGateway(), settings=one_fix)

    m = service.metrics(bracket(began))

    assert [w.verification_passed for w in m.work_times] == [True, True, False, False]
    assert m.judged_drafts == 4
    assert m.agreeing_drafts == 2


def test_the_report_shows_measured_values_against_baseline_and_targets(db, settings):
    began = now()
    review_one(db, settings, ALL_KEEP, ReviewDecision.CONFIRM,
               clock=SteppingClock(timedelta(minutes=12)))
    review_one(db, settings, REWRITE, ReviewDecision.CONFIRM,
               clock=SteppingClock(timedelta(minutes=12)))
    service = TaskDesignService(db=db, gateway=FakeGateway(), settings=settings)

    text = render_report(service.metrics(bracket(began)))

    assert "검토한 초안 2개" in text
    assert "| 전면 재작성 없이 통과한 비율 | — | 70% 이상 | 50% (1/2) | 목표 미달 |" in text
    assert ("| 과제 1개당 총 작업 시간 | 60분 | 42분 이하 | 24.0분 (사람 0.0 + 에이전트 24.0) | "
            "목표 달성 (수작업 기준선 60분 대비 60% 단축) |") in text
    assert "자동 검증(모의 응시 판정)과 사람 검토의 일치: 2개 중 1개 (50%)" in text


def test_the_report_says_so_when_nothing_was_reviewed(service):
    text = render_report(service.metrics(period_of_days(date(2001, 1, 1), date(2001, 1, 31))))

    assert "(2001.01.01 ~ 2001.01.31, 검토한 초안 0개)" in text
    assert "실측값이 없습니다" in text
    assert "|" not in text  # no table of zeros


def test_report_command_prints_the_period_it_was_asked_for(capsys, settings):
    code = main(["report", "--from", "2001-01-01", "--to", "2001-01-31"], settings=settings)

    assert code == 0
    assert "(2001.01.01 ~ 2001.01.31, 검토한 초안 0개)" in capsys.readouterr().out


def test_report_command_defaults_to_the_last_30_days(capsys, settings):
    code = main(["report"], settings=settings)

    today = datetime.now(KST).date()
    assert code == 0
    assert f"({today - timedelta(days=29):%Y.%m.%d} ~ {today:%Y.%m.%d}," in capsys.readouterr().out


def test_report_command_rejects_a_period_that_ends_before_it_starts(capsys, settings):
    code = main(["report", "--from", "2001-02-01", "--to", "2001-01-01"], settings=settings)

    assert code == 2
    assert "시작일" in capsys.readouterr().err
