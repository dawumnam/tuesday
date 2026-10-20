"""Korean prompts for the workflow's model calls.

Tests never assert on these strings; change wording freely. The offline fakes
(데모 모드, test helpers) only read the markers below, so keep a prompt using its
marker constant rather than spelling the marker out.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from uuid import UUID

from task_design.domain import (
    Attachment,
    ChatTurn,
    Competency,
    Component,
    DraftInput,
    Level,
    LevelIndicator,
    Rubric,
    Task,
)
from task_design.gateway import Message


# --- Markers: what offline fakes read to answer like a model ----------------------

JOB_FIELD = "- 직무: "
CAREER_FIELD = "- 경력 수준: "
DIFFICULTY_FIELD = "- 목표 난이도: "
AVOID_HEADING = "## 피해야 할 상황"  # 상황 후보: scenarios not to offer again
INSTRUCTION_HEADING = "## 지시문"
# Present only when that component is being rewritten (자기 수정 or 검토 에이전트).
PREVIOUS_HEADING = {
    Component.INSTRUCTION: "## 이전 지시문",
    Component.ATTACHMENTS: "## 이전 첨부 자료",
    Component.RUBRIC: "## 이전 채점 기준",
}
COMPETENCY_IDS_FIELD = "- 각 항목의 competency_id는 다음 중 하나입니다: "
TRANSCRIPT_HEADING = "## 제출물: AI 도구 대화 기록"
EXAMINEE_PREFIX = "[응시자]"  # starts each 응시자 line of a transcript being graded
TASK_ID_PATTERN = re.compile(r"\[과제 ID: ([0-9a-f-]{36})\]")  # a 유사 과제 shown to the model
RUBRIC_POINTS_PATTERN = re.compile(r"^\d+\. \[(?:과정|산출물)\] .*?\((\d+)점, 역량", re.MULTILINE)


def task_id_marker(task_id: UUID | None) -> str:
    return f"[과제 ID: {task_id}]"


def field(prompt: str, label: str) -> str | None:
    """Value of a ``- label: value`` line (``label`` is one of the ``*_FIELD`` markers)."""
    m = re.search(rf"^{re.escape(label)}(.+)$", prompt, re.MULTILINE)
    return m.group(1).strip() if m else None


def section(prompt: str, heading: str) -> str:
    """Body under a ``## heading`` (one of the ``*_HEADING`` markers), up to the next one."""
    m = re.search(rf"^{re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", prompt, re.MULTILINE | re.S)
    return m.group(1).strip() if m else ""


def is_rewrite(prompt: str, component: Component) -> bool:
    return PREVIOUS_HEADING[component] in prompt


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
        f"{task_id_marker(t.id)}\n상황: {t.scenario}\n지시문: {t.instruction}" for t in similar_tasks
    ) or "(참고할 기존 과제 없음)"
    avoid_block = (
        f"\n\n{AVOID_HEADING} (이미 제시했거나 기존 과제와 너무 비슷함)\n"
        + "\n".join(f"- {a}" for a in avoid)
        if avoid
        else ""
    )
    memo = f"\n- 메모: {input.memo}" if input.memo else ""
    user = f"""\
## 평가 역량
{_competency_lines(input, competencies)}

## 대상자와 난이도
{JOB_FIELD}{input.audience.job}
{CAREER_FIELD}{input.audience.career_level}
{DIFFICULTY_FIELD}{input.difficulty.ko}{memo}

## 참고할 기존 과제 (유사 과제)
{refs}{avoid_block}

## 요청
위 역량을 평가할 수 있는 상황 후보 {count}개를 제안하세요.
- 후보끼리 업무 맥락과 목표가 서로 충분히 달라야 합니다.
- 기존 과제를 참고하되 그대로 베끼지 마세요.
- 각 후보에는 짧은 제목(title), 상황 서술(scenario, 3~6문장), 참고한 기존 과제의 ID 목록
  (referenced_task_ids, 위 목록에 있는 ID만)을 넣으세요."""
    return [Message(role="system", content=SCENARIO_SYSTEM), Message(role="user", content=user)]


# --- 생성: 지시문 → 첨부 자료 → 채점 기준 -----------------------------------------

GENERATION_SYSTEM = """\
당신은 AI 사용능력 평가 과제를 설계하는 전문가입니다. 과제는 상황, 지시문, 첨부 자료,
채점 기준으로 이루어집니다. 응시자는 AI 도구를 사용해 과제를 풀고, 대화 기록과 최종 산출물을
제출합니다. 모든 내용은 한국어로 작성합니다."""


def _brief(input: DraftInput, competencies: Sequence[Competency], scenario: str) -> str:
    memo = f"\n- 메모: {input.memo}" if input.memo else ""
    return f"""\
## 평가 역량
{_competency_lines(input, competencies)}

## 대상자와 난이도
{JOB_FIELD}{input.audience.job}
{CAREER_FIELD}{input.audience.career_level}
{DIFFICULTY_FIELD}{input.difficulty.ko}{memo}

## 상황 (담당자가 고른 것, 바꾸지 마세요)
{scenario}"""


def _issues_block(title: str, issues: Sequence[str]) -> str:
    if not issues:
        return ""
    return f"\n\n## {title}\n" + "\n".join(f"- {i}" for i in issues)


def _attachments_text(attachments: Sequence[Attachment], *, with_traps: bool) -> str:
    if not attachments:
        return "(첨부 자료 없음)"
    parts = []
    for a in attachments:
        part = f"### {a.filename} ({a.format.ko})\n{a.content}"
        if with_traps and a.traps:
            part += "\n함정:\n" + "\n".join(f"- {t.location}: {t.description}" for t in a.traps)
        parts.append(part)
    return "\n\n".join(parts)


def rubric_text(rubric: Rubric) -> str:
    return "\n".join(
        f"{i}. [{item.type.ko}] {item.title} ({item.points}점, 역량 {item.competency_id})\n"
        f"   - 0점: {item.criteria_zero}\n   - 부분: {item.criteria_partial}\n"
        f"   - 만점: {item.criteria_full}"
        for i, item in enumerate(rubric.items)
    )


def instruction(
    input: DraftInput,
    competencies: Sequence[Competency],
    scenario: str,
    *,
    previous: str | None = None,
    issues: Sequence[str] = (),
) -> list[Message]:
    """지시문 prompt. ``previous`` + ``issues``: rewrite an existing one to fix these problems."""
    prev = f"\n\n{PREVIOUS_HEADING[Component.INSTRUCTION]}\n{previous}" if previous else ""
    user = f"""\
{_brief(input, competencies, scenario)}{prev}{_issues_block("고쳐야 할 점", issues)}

## 요청
이 상황에서 응시자가 AI 도구를 활용해 수행할 지시문(instruction)을 작성하세요.
- 수행할 작업과 제출물(최종 산출물의 형식)을 분명히 밝힙니다.
- 첨부 자료를 검토해야 풀 수 있게 합니다.
- 목표 난이도에 맞춥니다."""
    return [Message(role="system", content=GENERATION_SYSTEM), Message(role="user", content=user)]


def attachments(
    input: DraftInput,
    competencies: Sequence[Competency],
    scenario: str,
    instruction: str,
    *,
    previous: Sequence[Attachment] = (),
    issues: Sequence[str] = (),
) -> list[Message]:
    """첨부 자료 prompt. ``previous`` + ``issues``: rewrite the existing ones (and their 함정)."""
    prev = (f"\n\n{PREVIOUS_HEADING[Component.ATTACHMENTS]}\n"
            f"{_attachments_text(previous, with_traps=True)}"
            if previous else "")
    user = f"""\
{_brief(input, competencies, scenario)}

{INSTRUCTION_HEADING}
{instruction}{prev}{_issues_block("고쳐야 할 점", issues)}

## 요청
과제에 필요한 텍스트 첨부 자료(attachments)를 1~3개 작성하세요.
- 형식(format)은 csv, memo, email, report 중 하나입니다. CSV는 실제 CSV 텍스트로 씁니다.
- 응시자의 검증 행동을 드러낼 함정(traps)을 심고, 각 함정의 위치(location)와 내용(description)을
  표시하세요. 함정은 AI 출력을 그대로 믿으면 놓치기 쉬운 오류나 누락이어야 합니다."""
    return [Message(role="system", content=GENERATION_SYSTEM), Message(role="user", content=user)]


def rubric(
    input: DraftInput,
    competencies: Sequence[Competency],
    scenario: str,
    instruction: str,
    attachments: Sequence[Attachment],
    *,
    previous: Rubric | None = None,
    issues: Sequence[str] = (),
) -> list[Message]:
    """채점 기준 prompt. ``previous`` + ``issues``: rewrite an existing one to fix these problems."""
    prev = f"\n\n{PREVIOUS_HEADING[Component.RUBRIC]}\n{rubric_text(previous)}" if previous else ""
    ids = ", ".join(input.competency_ids)
    user = f"""\
{_brief(input, competencies, scenario)}

{INSTRUCTION_HEADING}
{instruction}

## 첨부 자료
{_attachments_text(attachments, with_traps=True)}{prev}{_issues_block("고쳐야 할 점", issues)}

## 요청
채점 기준(items)을 작성하세요.
- 항목은 3~5개, 배점(points) 합계는 100점입니다.
- 과정 항목(type=process, AI 사용 과정인 대화 기록을 판정)을 1개 이상, 산출물 항목(type=output)을
  1개 이상 둡니다. 함정을 발견했는지가 점수에 드러나게 합니다.
{COMPETENCY_IDS_FIELD}{ids}
- 각 항목에 0점·부분 점수·만점 판정 기준(criteria_zero, criteria_partial, criteria_full)을
  채점자마다 판단이 갈리지 않도록 관찰 가능한 행동으로 씁니다."""
    return [Message(role="system", content=GENERATION_SYSTEM), Message(role="user", content=user)]


def task_text(task: Task, *, with_traps: bool = True) -> str:
    return f"""\
## 상황
{task.scenario}

{INSTRUCTION_HEADING}
{task.instruction}

## 첨부 자료
{_attachments_text(task.attachments, with_traps=with_traps)}"""


REVIEW_SYSTEM = """\
당신은 AI 사용능력 평가 과제의 검토 에이전트입니다. 지시문과 채점 기준이 서로 맞는지 점검합니다."""


def review(task: Task) -> list[Message]:
    user = f"""\
{task_text(task)}

## 채점 기준
{rubric_text(task.rubric)}

## 요청
지시문과 채점 기준의 정합성을 점검하세요.
- 채점 기준이 지시문에서 요구하지 않은 것을 채점하거나, 지시문이 요구한 제출물을 채점 기준이
  다루지 않으면 불일치입니다.
- 일치하면 consistent=true. 불일치하면 consistent=false, 다시 써야 할 쪽(target: instruction 또는
  rubric)과 구체적인 문제(issues)를 적으세요."""
    return [Message(role="system", content=REVIEW_SYSTEM), Message(role="user", content=user)]


# --- 모의 응시 ---------------------------------------------------------------------

PERSONA_BEHAVIOUR = {
    Level.LOW: "지시문을 그대로 AI 도구에 붙여넣고, 첫 출력을 확인하지 않고 바로 제출합니다.",
    Level.MEDIUM: "맥락과 원하는 형식을 지정해 요청하고, 결과를 보고 1~2회 고쳐 달라고 요청한 뒤 "
    "제출합니다.",
    Level.HIGH: "작업을 작은 단계로 쪼개 요청하고, 첨부 자료의 사실과 수치를 직접 대조해 함정을 "
    "확인하며, 결과를 반복해서 개선한 뒤 제출합니다.",
}


def persona_tag(level: Level) -> str:
    """Line in every persona prompt naming its level (stable; test helpers look for it)."""
    return f"응시자 수준: {level.ko}"


def persona_turn(
    level: Level,
    input: DraftInput,
    indicators: Sequence[LevelIndicator],
    task: Task,
    transcript: Sequence[ChatTurn],
    *,
    max_turns: int,
) -> list[Message]:
    """Persona prompt: its own messages are ``assistant``, the AI 도구's replies ``user``."""
    lines = "\n".join(f"- {i.sub_element_name}: {i.description}" for i in indicators) or "- (없음)"
    system = f"""\
당신은 AI 사용능력 평가에 응시하는 사람을 연기합니다.
- 직무: {input.audience.job}, 경력 수준: {input.audience.career_level}
- {persona_tag(level)}

## 이 수준의 AI 사용 행동 (수준 지표)
{lines}

## 행동 방식
{PERSONA_BEHAVIOUR[level]}

## 규칙
- 과제는 AI 도구와 대화하며 풉니다. AI 도구에 보낼 수 있는 메시지는 최대 {max_turns}개입니다.
- 매 차례 action=ask(AI 도구에 보낼 메시지를 content에) 또는 action=submit(최종 산출물을
  content에) 중 하나를 고릅니다.
- 위 수준보다 잘하거나 못하게 행동하지 마세요."""
    msgs = [
        Message(role="system", content=system),
        Message(role="user", content=f"{task_text(task, with_traps=False)}\n\n과제를 시작하세요."),
    ]
    for turn in transcript:
        if turn.speaker == "examinee":
            msgs.append(Message(role="assistant", content=turn.content))
        else:
            msgs.append(Message(role="user", content=f"[AI 도구 응답]\n{turn.content}"))
    left = max_turns - sum(t.speaker == "examinee" for t in transcript)
    nudge = (
        f"다음 행동을 고르세요. 남은 메시지 수: {left}"
        if left > 0
        else "더 이상 AI 도구에 메시지를 보낼 수 없습니다. 지금 최종 산출물을 제출하세요(action=submit)."
    )
    msgs.append(Message(role="user", content=nudge))
    return msgs


AI_TOOL_SYSTEM = "당신은 업무를 돕는 범용 AI 어시스턴트입니다. 사용자의 요청에 한국어로 답합니다."


def ai_tool_turn(transcript: Sequence[ChatTurn]) -> list[Message]:
    msgs = [Message(role="system", content=AI_TOOL_SYSTEM)]
    for turn in transcript:
        role = "user" if turn.speaker == "examinee" else "assistant"
        msgs.append(Message(role=role, content=turn.content))
    return msgs


GRADING_SYSTEM = """\
당신은 AI 사용능력 평가의 채점자입니다. 채점 기준의 판정 서술에만 근거해 제출물을 채점합니다."""


def grading(task: Task, transcript: Sequence[ChatTurn], output: str) -> list[Message]:
    log = "\n".join(
        f"{EXAMINEE_PREFIX if t.speaker == 'examinee' else '[AI 도구]'} {t.content}"
        for t in transcript
    ) or "(대화 없음)"
    user = f"""\
{task_text(task)}

## 채점 기준
{rubric_text(task.rubric)}

{TRANSCRIPT_HEADING}
{log}

## 제출물: 최종 산출물
{output}

## 요청
채점 기준의 각 항목(item_index는 위 번호, 0부터)에 대해 0점부터 배점 사이의 점수(score)와
근거(rationale)를 매기세요. 과정 항목은 대화 기록으로, 산출물 항목은 최종 산출물로 판정합니다."""
    return [Message(role="system", content=GRADING_SYSTEM), Message(role="user", content=user)]
