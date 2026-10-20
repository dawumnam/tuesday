"""Korean prompts for the workflow's model calls.

Tests never assert on these strings; change wording freely.
"""

from __future__ import annotations

from collections.abc import Sequence

from task_design.domain import Competency, DraftInput, Task
from task_design.gateway import Message


def _competency_lines(input: DraftInput, competencies: Sequence[Competency]) -> str:
    by_id = {c.id: c for c in competencies}
    lines = []
    for cid in input.competency_ids:
        c = by_id.get(cid)
        if c is None:
            lines.append(f"- {cid}")
            continue
        lines.append(f"- {c.name}: {c.description}".rstrip(": "))
        lines.extend(f"  - {s.name}: {s.description}".rstrip(": ") for s in c.sub_elements)
    return "\n".join(lines)


def similar_task_query(input: DraftInput, competencies: Sequence[Competency]) -> str:
    """검색어 for 유사 과제 검색 (trigram + vector + rerank)."""
    names = {c.id: c.name for c in competencies}
    parts = [names.get(cid, cid) for cid in input.competency_ids]
    parts += [input.audience.job, input.audience.career_level]
    if input.memo:
        parts.append(input.memo)
    return " ".join(parts)


SCENARIO_SYSTEM = """\
당신은 AI 사용능력 평가 과제를 설계하는 전문가입니다.
평가 대상자가 실제 업무 중에 마주칠 법한 '상황'을 설계합니다.
상황은 업무 맥락, 등장 인물, 목표를 담은 과제의 도입부이며, 응시자가 AI 도구를 활용해야
해결할 수 있는 일이어야 합니다. 모든 내용은 한국어로 작성합니다."""


def scenario_candidates(
    input: DraftInput,
    competencies: Sequence[Competency],
    similar_tasks: Sequence[Task],
    *,
    count: int,
    avoid: Sequence[str] = (),
) -> list[Message]:
    """상황 후보 생성 프롬프트. ``avoid``: scenarios that must not be repeated or imitated."""
    refs = "\n\n".join(
        f"[과제 ID: {t.id}]\n상황: {t.scenario}\n지시문: {t.instruction}" for t in similar_tasks
    ) or "(참고할 기존 과제 없음)"
    avoid_block = (
        "\n\n## 피해야 할 상황 (이미 제시했거나 기존 과제와 너무 비슷함)\n"
        + "\n".join(f"- {a}" for a in avoid)
        if avoid
        else ""
    )
    memo = f"\n- 메모: {input.memo}" if input.memo else ""
    user = f"""\
## 평가 역량
{_competency_lines(input, competencies)}

## 대상자와 난이도
- 직무: {input.audience.job}
- 경력 수준: {input.audience.career_level}
- 목표 난이도: {input.difficulty.ko}{memo}

## 참고할 기존 과제 (유사 과제)
{refs}{avoid_block}

## 요청
위 역량을 평가할 수 있는 상황 후보 {count}개를 제안하세요.
- 후보끼리 업무 맥락과 목표가 서로 충분히 달라야 합니다.
- 기존 과제를 참고하되 그대로 베끼지 마세요.
- 각 후보에는 짧은 제목(title), 상황 서술(scenario, 3~6문장), 참고한 기존 과제의 ID 목록
  (referenced_task_ids, 위 목록에 있는 ID만)을 넣으세요."""
    return [Message(role="system", content=SCENARIO_SYSTEM), Message(role="user", content=user)]
