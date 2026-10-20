"""측정: 측정 이벤트 storage and the metrics computation behind ``TaskDesignService.metrics``.

Rules (decided for ticket 09; see also GLOSSARY 총 작업 시간 / 전면 재작성):

- **Period.** Review metrics (ratio, 총 작업 시간, label distribution) cover drafts
  whose review (``reviews.reviewed_at``) falls in the period, start inclusive and end
  exclusive. Unreviewed drafts are left out of the ratio and the time metric.
  Cost metrics cover every draft *created* in the period, reviewed or not, with
  all of that draft's model calls.
- **Pass ratio** = drafts with decision 확정 and no 전면 재작성 label on any
  component / all drafts reviewed (확정 + 반려). 반려 counts as a failure.
- **총 작업 시간** per reviewed draft = union of its 사람 활성 intervals (across all
  staff, so overlaps count once) + union of its 에이전트 실행 intervals. Gaps between
  intervals (e.g. inactive tabs) are not counted. All of a draft's intervals count,
  whenever they happened. Reported per draft and as the mean.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from task_design.costs import CostStore
from task_design.db import Database
from task_design.domain import (
    LABELED_COMPONENTS,
    Cost,
    DraftCost,
    DraftWorkTime,
    Interval,
    IntervalKind,
    Metrics,
    ReviewDecision,
    ReviewLabel,
)
from task_design.settings import Settings
from task_design.tracing import trace_url


def union_minutes(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    """Length of the union of intervals, in minutes (overlaps count once)."""
    total = 0.0
    cur_start = cur_end = None
    for start, end in sorted(intervals):
        if cur_end is None or start > cur_end:
            if cur_end is not None:
                total += (cur_end - cur_start).total_seconds()
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    if cur_end is not None:
        total += (cur_end - cur_start).total_seconds()
    return total / 60


class MeasurementStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self, draft_id: UUID, staff_id: int | None, kind: IntervalKind, interval: Interval
    ) -> None:
        with self.db.connection() as conn:
            conn.execute(
                """
                INSERT INTO measurement_events (draft_id, staff_id, kind, started_at, ended_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (draft_id, staff_id, kind.value, interval.start, interval.end),
            )

    def intervals(
        self, draft_ids: list[UUID]
    ) -> dict[tuple[UUID, IntervalKind], list[tuple[datetime, datetime]]]:
        out: dict[tuple[UUID, IntervalKind], list[tuple[datetime, datetime]]] = defaultdict(list)
        if not draft_ids:
            return out
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT draft_id, kind, started_at, ended_at FROM measurement_events "
                "WHERE draft_id = ANY(%s)",
                (draft_ids,),
            ).fetchall()
        for draft_id, kind, start, end in rows:
            out[(draft_id, IntervalKind(kind))].append((start, end))
        return out


def compute_metrics(db: Database, settings: Settings, period: Interval) -> Metrics:
    bounds = {"start": period.start, "end": period.end}
    with db.connection() as conn:
        reviews = conn.execute(
            """
            SELECT r.draft_id, r.decision, r.reviewed_at,
                   EXISTS (SELECT 1 FROM review_labels l
                           WHERE l.draft_id = r.draft_id AND l.label = 'full_rewrite')
            FROM reviews r
            WHERE r.reviewed_at >= %(start)s AND r.reviewed_at < %(end)s
            ORDER BY r.reviewed_at, r.draft_id
            """,
            bounds,
        ).fetchall()
        label_rows = conn.execute(
            """
            SELECT l.component, l.label, count(*)
            FROM reviews r JOIN review_labels l USING (draft_id)
            WHERE r.reviewed_at >= %(start)s AND r.reviewed_at < %(end)s
            GROUP BY 1, 2
            """,
            bounds,
        ).fetchall()
        created = conn.execute(
            "SELECT id, created_at FROM drafts "
            "WHERE created_at >= %(start)s AND created_at < %(end)s ORDER BY created_at, id",
            bounds,
        ).fetchall()

    intervals = MeasurementStore(db).intervals([r[0] for r in reviews])
    work_times = []
    for draft_id, decision, reviewed_at, full_rewrite in reviews:
        decision = ReviewDecision(decision)
        human = union_minutes(intervals[(draft_id, IntervalKind.HUMAN_ACTIVE)])
        agent = union_minutes(intervals[(draft_id, IntervalKind.AGENT_RUN)])
        work_times.append(
            DraftWorkTime(
                draft_id=draft_id,
                decision=decision,
                passed=decision is ReviewDecision.CONFIRM and not full_rewrite,
                reviewed_at=reviewed_at,
                human_minutes=human,
                agent_minutes=agent,
                total_minutes=human + agent,
                trace_url=trace_url(settings, draft_id),
            )
        )

    labels = {c: dict.fromkeys(ReviewLabel, 0) for c in LABELED_COMPONENTS}
    for component, label, n in label_rows:
        labels.setdefault(component, dict.fromkeys(ReviewLabel, 0))[ReviewLabel(label)] = n

    costs = CostStore(db).for_drafts([d for d, _ in created])
    draft_costs = _per_draft(settings, created, costs)

    reviewed = len(work_times)
    passed = sum(w.passed for w in work_times)
    ratio = passed / reviewed if reviewed else None
    mean = sum(w.total_minutes for w in work_times) / reviewed if reviewed else None
    return Metrics(
        period=period,
        reviewed_drafts=reviewed,
        passed_drafts=passed,
        pass_without_full_rewrite_ratio=ratio,
        pass_ratio_target=settings.pass_ratio_target,
        meets_pass_ratio_target=None if ratio is None else ratio >= settings.pass_ratio_target,
        mean_total_work_minutes=mean,
        work_minutes_target=settings.work_minutes_target,
        meets_work_minutes_target=None if mean is None else mean <= settings.work_minutes_target,
        work_times=work_times,
        label_distribution=labels,
        costs=costs,
        draft_costs=draft_costs,
        total_calls=sum(c.calls for c in draft_costs),
        total_cost_usd=sum(c.estimated_cost_usd for c in draft_costs),
    )


def _per_draft(
    settings: Settings, created: list[tuple[UUID, datetime]], costs: list[Cost]
) -> list[DraftCost]:
    by_draft: dict[UUID, list[Cost]] = defaultdict(list)
    for c in costs:
        by_draft[c.draft_id].append(c)
    return [
        DraftCost(
            draft_id=draft_id,
            created_at=created_at,
            calls=sum(c.calls for c in by_draft[draft_id]),
            input_tokens=sum(c.input_tokens for c in by_draft[draft_id]),
            output_tokens=sum(c.output_tokens for c in by_draft[draft_id]),
            estimated_cost_usd=sum(c.estimated_cost_usd for c in by_draft[draft_id]),
            trace_url=trace_url(settings, draft_id),
        )
        for draft_id, created_at in created
    ]
