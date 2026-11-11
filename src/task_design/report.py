"""성공 기준 실측 리포트: ``Metrics`` as a short Markdown text for a slide or a README.

Used by ``task-design report`` and the 대시보드 ("발표용 요약"). Only numbers the
system measured go in; with nothing reviewed it says so instead of showing zeros.
"""

from __future__ import annotations

from datetime import timedelta

from task_design.domain import Metrics
from task_design.measurement import KST


def _verdict(ok: bool | None) -> str:
    return "데이터 없음" if ok is None else ("목표 달성" if ok else "목표 미달")


def saved_text(m: Metrics) -> str | None:
    """'수작업 기준선 60분 대비 41% 단축' (or '… 12% 증가'); None without data."""
    saved = m.work_minutes_saved_ratio
    if saved is None:
        return None
    change = f"{saved * 100:.0f}% 단축" if saved >= 0 else f"{-saved * 100:.0f}% 증가"
    return f"수작업 기준선 {m.baseline_work_minutes:.0f}분 대비 {change}"


def agreement_text(m: Metrics) -> str | None:
    """How often 자동 검증 and the human review agree; None when no draft was judged."""
    if not m.judged_drafts:
        return None
    return (f"자동 검증(모의 응시 판정)과 사람 검토의 일치: {m.judged_drafts}개 중 "
            f"{m.agreeing_drafts}개 ({m.agreeing_drafts / m.judged_drafts * 100:.0f}%)")


def render_report(m: Metrics) -> str:
    start = m.period.start.astimezone(KST).date()
    # The period's end is exclusive (normally the midnight after the last day).
    end = (m.period.end.astimezone(KST) - timedelta(microseconds=1)).date()
    lines = [f"## 성공 기준 실측 ({start:%Y.%m.%d} ~ {end:%Y.%m.%d}, 검토한 초안 "
             f"{m.reviewed_drafts}개)", ""]
    if not m.reviewed_drafts:
        lines.append("이 기간에 최종 검토(확정·반려)된 초안이 없어 실측값이 없습니다. "
                     "초안을 만들고 다른 담당자가 검토 라벨을 붙여 확정하거나 반려하면 집계됩니다.")
        return "\n".join(lines) + "\n"

    ratio = m.pass_without_full_rewrite_ratio or 0.0
    work = m.mean_total_work_minutes or 0.0
    work_result = _verdict(m.meets_work_minutes_target)
    if (saved := saved_text(m)) is not None:
        work_result += f" ({saved})"
    lines += [
        "| 지표 | 수작업 기준선 | 목표 | 실측 | 결과 |",
        "|---|---|---|---|---|",
        f"| 전면 재작성 없이 통과한 비율 | — | {m.pass_ratio_target * 100:.0f}% 이상 | "
        f"{ratio * 100:.0f}% ({m.passed_drafts}/{m.reviewed_drafts}) | "
        f"{_verdict(m.meets_pass_ratio_target)} |",
        f"| 과제 1개당 총 작업 시간 | {m.baseline_work_minutes:.0f}분 | "
        f"{m.work_minutes_target:.0f}분 이하 | {work:.1f}분 "
        f"(사람 {m.mean_human_minutes or 0:.1f} + 에이전트 {m.mean_agent_minutes or 0:.1f}) | "
        f"{work_result} |",
        "",
        "- 통과 = 다른 담당자가 전면 재작성 라벨 없이 확정한 초안. 반려는 실패로 셉니다.",
        "- 총 작업 시간 = 사람 시간(탭이 보이고 활동 중인 구간) + 에이전트 실행 시간.",
    ]
    if (agreement := agreement_text(m)) is not None:
        lines.append(f"- {agreement}")
    drafts = len(m.draft_costs)
    if drafts:
        lines.append(f"- LLM 비용(추정): 초안 {drafts}개에 ${m.total_cost_usd:.2f}, "
                     f"초안당 ${m.total_cost_usd / drafts:.3f} (호출 {m.total_calls:,}회)")
    return "\n".join(lines) + "\n"
