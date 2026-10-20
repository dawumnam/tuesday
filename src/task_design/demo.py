"""데모 모드 gateway: plausible Korean content, fully offline (``TD_FAKE_GATEWAY=1``).

``build_demo_gateway`` returns a ``FakeGateway`` whose responders read the
workflow's prompts and answer like a model would, so every screen can be clicked
through without an API key:

- 상황 후보: 3 distinct scenarios per call, built for the 대상자's 직무·경력 수준 from
  a pool of 9 themes, skipping anything in the prompt's "피해야 할 상황" list.
- Each theme carries its own 첨부 자료 (a CSV with a duplicated row and a 합계 row that
  counts it, plus a memo with an unsupported claim), so the 지시문, 첨부 자료, 함정,
  persona transcripts and outputs all fit the chosen 상황. A rewrite (the prompt
  carries the previous version) comes back visibly revised.
- 응시자 페르소나: 하 asks once and submits, 중 twice, 상 four times while checking the
  CSV. The grader tells the levels apart by the number of 응시자 turns.
- Verification outcomes, so the self-correction loop can be shown:
  난이도 상 → round 1 fails 변별력 and one 자기 수정 fixes it; a memo containing
  ``FAIL_MARKER`` → never passes, ending with a 실패 리포트; otherwise round 1 passes.

``delay`` seconds are slept before every completion so progress is watchable.
Per-draft outcome state lives in memory: after an app restart a resumed draft is
graded as a normal (passing) one.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from task_design.gateway import Role, UsageSink
from task_design.settings import DEFAULT_GEMINI_MODEL, Settings
from task_design.testing.fake_gateway import FakeCall, FakeGateway
from task_design.workflow.schemas import (
    AttachmentBatch,
    CandidateBatch,
    InstructionDraft,
    RubricDraft,
)

FAIL_MARKER = "실패 시연"

_IDS = re.compile(r"\[과제 ID: ([0-9a-f-]{36})\]")


def _field(prompt: str, label: str, default: str) -> str:
    m = re.search(rf"^- {label}: (.+)$", prompt, re.MULTILINE)
    return m.group(1).strip() if m else default


def _section(prompt: str, title: str) -> str:
    m = re.search(rf"^## {re.escape(title)}[^\n]*\n(.*?)(?=^## |\Z)", prompt, re.MULTILINE | re.S)
    return m.group(1).strip() if m else ""


# --- 주제 (상황 + 첨부 자료) ----------------------------------------------------------


@dataclass(frozen=True)
class Theme:
    title: str
    marker: str  # phrase that identifies the theme's scenario inside a prompt
    scenario: str  # {job}, {career}
    deliverable: str  # what the 지시문 asks for
    csv_name: str
    header: str  # "label,…,value" — the last column is the number that is totalled
    rows: tuple[str, ...]
    duplicate: int  # index into rows of the row that appears twice
    unit: str
    memo_name: str
    memo: str
    memo_trap: str


THEMES: tuple[Theme, ...] = (
    Theme("분기 실적 보고 자료 검증", "분기 실적 요약",
          "{job} 부서 {career} 담당자인 당신은 내일 오전 임원 회의에 올라갈 분기 실적 요약을 맡았다. "
          "팀장이 다른 팀원이 정리한 지역별 매출 CSV와 메모를 넘기며 'AI로 빨리 정리해 달라'고 했다. "
          "마감 직전에 급히 취합된 자료라 숫자를 그대로 믿기 어렵다.",
          "임원이 바로 읽을 한 쪽 분량의 분기 실적 요약 보고서",
          "분기_매출.csv", "지역,매출(만원)", ("서울,3960", "부산,2530", "대구,2020", "광주,1480"),
          1, "만원", "팀장_메모.txt",
          "이번 분기는 부산이 가장 크게 성장했으니 그 점을 강조해 주세요. 지난 분기보다 전체 매출이 "
          "15% 늘었습니다.",
          "부산 성장과 15% 증가는 자료로 확인할 수 없는 주장 (전 분기 자료 없음)"),
    Theme("고객 불만 대응 회신", "불만 이메일",
          "{job} 부서 {career} 담당자인 당신에게 주요 거래처의 불만 이메일이 한꺼번에 전달되었다. "
          "고객별 보상 요청 금액을 정리한 CSV와 사내 대응 지침 메모도 받았다. 오늘 안에 일관된 회신 "
          "초안과 보상 검토안을 보내야 한다.",
          "고객별 회신 초안과 보상 검토 요약",
          "보상_요청.csv", "고객사,요청 금액(만원)", ("한빛상사,320", "누리물산,180", "다온테크,450",
                                                  "새봄유통,90"),
          2, "만원", "대응_지침.txt",
          "보상 요청은 이번에 모두 승인하기로 결정되었습니다. 회신에 승인 사실을 바로 적어 주세요.",
          "승인 결정의 근거가 없음 (지침에 결재 절차가 빠져 있음)"),
    Theme("신규 협력사 비교 검토", "협력사 후보",
          "{job} 부서 {career} 담당자인 당신은 신규 협력사 후보를 비교하는 검토서를 써야 한다. "
          "구매팀이 견적을 한 표로 모은 CSV와 담당 임원의 메모를 보냈다. 다음 주 구매 위원회 전에 "
          "비교표와 추천 의견을 정리해야 한다.",
          "협력사 비교표와 추천 의견을 담은 한 쪽 검토서",
          "견적_비교.csv", "협력사,견적(만원)", ("가온솔루션,4200", "바른시스템,3900",
                                            "세움정보,4500"),
          1, "만원", "임원_메모.txt",
          "바른시스템이 가장 비싸니 후보에서 빼는 게 좋겠습니다.",
          "바른시스템은 실제로 가장 저렴함 (메모의 판단이 자료와 반대)"),
    Theme("주간 업무 보고 정리", "주간 업무 보고",
          "{job} 부서 {career} 담당자인 당신은 매주 반복되는 주간 업무 보고를 정리한다. "
          "팀원별 투입 시간을 모은 CSV와 팀장 메모가 있다. AI 도구로 이번 주 보고서를 만들고, "
          "다음 주에도 쓸 요청 문구를 남겨야 한다.",
          "이번 주 업무 보고서와 다음 주에 재사용할 요청 문구",
          "투입_시간.csv", "팀원,투입 시간(시간)", ("김하늘,38", "이도윤,42", "박서연,35",
                                                "최지우,40", "정민준,36"),
          3, "시간", "팀장_메모.txt",
          "이번 주 팀 투입 시간이 지난주보다 20% 늘었으니 인력 충원 근거로 써 주세요.",
          "지난주 자료가 없어 20% 증가는 확인할 수 없는 주장"),
    Theme("교육 만족도 설문 분석", "만족도 설문",
          "{job} 부서 {career} 담당자인 당신은 지난달 사내 교육의 만족도 설문 결과를 분석해야 한다. "
          "과정별 응답 수를 모은 CSV와 교육팀 메모가 있는데, 일부 응답이 중복 집계된 것으로 보인다. "
          "교육 담당 임원에게 개선 방향을 담은 보고서를 올려야 한다.",
          "과정별 결과와 개선 방향을 담은 한 쪽 보고서",
          "설문_응답.csv", "과정,응답 수(명)", ("AI 기초,64", "데이터 분석,41", "보고서 작성,37",
                                             "프롬프트 실습,52"),
          0, "명", "교육팀_메모.txt",
          "전체 응답률이 90%를 넘었으니 결과를 전사 대표 의견으로 보고해 주세요.",
          "수강 인원 자료가 없어 응답률 90%는 확인할 수 없는 주장"),
    Theme("외부 공유용 고객 현황 정리", "고객 현황 자료",
          "{job} 부서 {career} 담당자인 당신은 외부 컨설팅사와 공유할 고객 현황 자료를 준비한다. "
          "원본 CSV에는 고객 담당자 연락처가 그대로 들어 있고, 공유 기한은 오늘 오후다. "
          "AI 도구로 요약 자료를 만들되 외부 공유 기준을 지켜야 한다.",
          "외부 공유 기준을 지킨 고객 현황 요약",
          "고객_현황.csv", "고객사,담당자 연락처,계약 금액(만원)",
          ("한빛상사,010-1234-5678,2400", "누리물산,010-2345-6789,1800",
           "다온테크,010-3456-7890,3100"),
          0, "만원", "영업팀_메모.txt",
          "연락처는 컨설팅사도 필요하다고 하니 그대로 넘겨도 됩니다.",
          "개인정보(연락처)를 외부에 넘기면 안 됨 (공유 기준 위반)"),
    Theme("상반기 예산 집행 점검", "예산 집행 현황",
          "{job} 부서 {career} 담당자인 당신은 상반기 예산 집행 현황을 점검하라는 요청을 받았다. "
          "재무팀이 보낸 집행 내역 CSV와 부서장 메모가 있다. 집행률을 정리하고 이상 항목을 찾아 "
          "보고해야 한다.",
          "항목별 집행 현황과 이상 항목을 정리한 점검 보고서",
          "집행_내역.csv", "항목,집행액(만원)", ("교육비,820", "출장비,1340", "외주 용역비,2600",
                                            "소모품비,310"),
          2, "만원", "부서장_메모.txt",
          "외주 용역비는 계획보다 적게 썼으니 따로 설명하지 않아도 됩니다.",
          "계획 금액 자료가 없어 '적게 썼다'는 확인할 수 없는 주장"),
    Theme("전략 회의 실행 계획 정리", "녹취 요약본",
          "{job} 부서 {career} 담당자인 당신은 전략 회의의 녹취 요약본과 실행 항목별 예상 비용 CSV를 "
          "받았다. 결정 사항과 논의 중인 사항이 섞여 있고 담당자 이름이 일부 빠져 있다. 실행 계획표를 "
          "만들어 오늘 참석자들에게 공유해야 한다.",
          "담당자·기한·예상 비용이 들어간 실행 계획표",
          "실행_항목.csv", "실행 항목,예상 비용(만원)", ("고객 설문 개편,600", "대시보드 구축,1500",
                                                    "영업 교육,450", "파일럿 운영,900"),
          1, "만원", "회의_요약.txt",
          "모든 실행 항목은 회의에서 최종 확정되었습니다.",
          "일부 항목은 '논의 중'이었음 (확정되지 않은 항목이 섞여 있음)"),
    Theme("경쟁사 동향 브리핑", "경쟁사 동향",
          "{job} 부서 {career} 담당자인 당신은 경쟁사 동향 브리핑을 준비한다. 시장 점유율을 모은 "
          "CSV와 기사 요약 메모가 있는데, 출처와 날짜가 서로 다른 자료가 섞여 있다. 핵심만 추린 "
          "브리핑을 만들되 근거가 약한 주장은 걸러야 한다.",
          "근거를 밝힌 한 쪽 경쟁사 동향 브리핑",
          "시장_점유율.csv", "회사,점유율(%)", ("우리 회사,31", "알파,27", "베타,22", "기타,20"),
          2, "%", "기사_요약.txt",
          "베타가 곧 시장 1위가 될 것이라는 보도가 있습니다.",
          "출처와 날짜가 없는 전망 기사 (근거가 약함)"),
)


def theme_of(text: str) -> Theme:
    """The theme a prompt or conversation is about (by its 상황, file name or CSV header)."""
    return next((t for t in THEMES if t.marker in text or t.csv_name in text or t.header in text),
                THEMES[0])


@dataclass(frozen=True)
class Numbers:
    label: str  # the duplicated row's label
    stated: int  # 합계 row (counts the duplicate)
    actual: int  # without the duplicate
    top: str  # label with the largest value


def numbers(theme: Theme) -> Numbers:
    values = [(r.split(",")[0], int(r.split(",")[-1])) for r in theme.rows]
    dup_label, dup_value = values[theme.duplicate]
    actual = sum(v for _, v in values)
    return Numbers(dup_label, actual + dup_value, actual, max(values, key=lambda x: x[1])[0])


def csv_text(theme: Theme, *, revised: bool = False) -> str:
    rows = list(theme.rows)
    rows.insert(theme.duplicate + 2 if theme.duplicate + 2 <= len(rows) else len(rows),
                theme.rows[theme.duplicate])
    n = numbers(theme)
    cols = theme.header.count(",")
    total = ",".join(["합계", *([""] * (cols - 1)), str(n.stated)])
    if revised:  # 함정 rewrite: also a typo'd value
        first = rows[0].rsplit(",", 1)
        rows[0] = f"{first[0]},{int(first[1]) * 10}"
    return "\n".join([theme.header, *rows, total])


# --- 상황 후보 -----------------------------------------------------------------


def _candidates(call: FakeCall) -> dict:
    prompt = call.prompt
    job = _field(prompt, "직무", "사업")
    career = _field(prompt, "경력 수준", "3년차")
    avoid = _section(prompt, "피해야 할 상황")
    ids = _IDS.findall(prompt)
    out, variant = [], 0
    while len(out) < 3:
        for i, theme in enumerate(THEMES):
            suffix = f" (변형 {variant})" if variant else ""
            scenario = theme.scenario.format(job=job, career=career) + suffix
            if scenario in avoid or any(c["scenario"] == scenario for c in out):
                continue
            refs = [ids[(i + k) % len(ids)] for k in range(min(2, len(ids)))] if ids else []
            out.append({"title": theme.title + suffix, "scenario": scenario,
                        "referenced_task_ids": list(dict.fromkeys(refs))})
            if len(out) == 3:
                break
        variant += 1
    return {"candidates": out}


# --- 지시문 · 첨부 자료 · 채점 기준 ------------------------------------------------------


def _instruction(call: FakeCall) -> dict:
    theme = theme_of(call.prompt)
    text = (
        f"첨부한 {theme.csv_name}와 {theme.memo_name}를 AI 도구로 분석해 {theme.deliverable}를 "
        "작성하라. 핵심 수치 3개 이상, 자료에서 발견한 문제점과 그 근거, 다음에 할 일 제안을 "
        "포함한다. 제출물은 AI 도구와의 대화 기록 전체와 최종 산출물이다."
    )
    if "## 이전 지시문" in call.prompt:
        text += (
            "\n\n작업 순서: ① 첨부 자료의 구조와 항목을 먼저 확인한다. ② 합계·중복 등 수치를 AI "
            "결과와 직접 대조한다. ③ 검증한 수치만 산출물에 쓴다. 형식은 '요약 - 근거 - 제안' "
            "세 단락으로 한다."
        )
    return {"instruction": text}


def _attachments(call: FakeCall) -> dict:
    theme = theme_of(call.prompt)
    revised = "## 이전 첨부 자료" in call.prompt
    n = numbers(theme)
    traps = [
        {"location": f"{n.label} 행", "description": f"{n.label} 행이 한 번 더 들어간 중복 행"},
        {"location": "마지막 합계 행",
         "description": f"합계 {n.stated:,}{theme.unit}는 중복 행까지 더한 값 (실제 {n.actual:,})"},
    ]
    if revised:
        traps.append({"location": "2행", "description": "값에 0이 하나 더 붙은 입력 오류"})
    return {"attachments": [
        {"filename": theme.csv_name, "format": "csv", "content": csv_text(theme, revised=revised),
         "traps": traps},
        {"filename": theme.memo_name, "format": "memo", "content": theme.memo,
         "traps": [{"location": "메모 본문", "description": theme.memo_trap}]},
    ]}


def _rubric(call: FakeCall) -> dict:
    revised = "## 이전 채점 기준" in call.prompt
    m = re.search(r"competency_id는 다음 중 하나입니다: (.+)$", call.prompt, re.MULTILINE)
    ids = [c.strip() for c in m.group(1).split(",")] if m else ["C1"]
    sharper = " 근거가 된 행을 하나 이상 적어야 만점이다." if revised else ""
    items = [
        ("AI 결과 검증 과정", "process", 30,
         "AI가 낸 수치를 확인하지 않고 그대로 사용했다.",
         "일부 수치만 원자료와 대조했거나 함정을 1개만 발견했다.",
         "중복 행·합계 오류와 메모의 근거 없는 주장을 모두 원자료와 대조해 찾아냈다." + sharper),
        ("요청 구체화", "process", 20,
         "지시문을 그대로 붙여넣는 요청만 했다.",
         "형식이나 맥락 중 하나만 지정해 요청했다.",
         "목적·형식·제약을 지정하고 결과를 보며 요청을 고쳤다."),
        ("수치 정확성", "output", 30,
         "산출물 수치에 함정이 반영된 오류가 있다.",
         "오류 일부만 바로잡았다.",
         "모든 핵심 수치가 중복을 뺀 원자료와 일치한다." + sharper),
        ("구성과 전달력", "output", 20,
         "요약·근거·제안 중 둘 이상이 빠졌다.",
         "구성은 갖췄으나 근거가 약하다.",
         "요약·근거·제안이 분명하고 한 쪽 분량을 지켰다."),
    ]
    return {"items": [
        {"title": title, "competency_id": ids[i % len(ids)], "type": type_, "points": points,
         "criteria_zero": zero, "criteria_partial": partial, "criteria_full": full}
        for i, (title, type_, points, zero, partial, full) in enumerate(items)
    ]}


# --- 모의 응시 -----------------------------------------------------------------


def _asks(level: str, theme: Theme, instruction: str) -> list[str]:
    if level == "하":
        return [f"아래 과제를 해 줘.\n\n{instruction}"]
    if level == "중":
        return [f"첨부한 {theme.csv_name}와 메모로 {theme.deliverable}를 써 줘. "
                "요약 - 근거 - 제안 형식으로.",
                "핵심 수치를 표로 먼저 보여 주고, 문장을 조금 더 간결하게 고쳐 줘."]
    return [f"먼저 아래 CSV의 열 구성과 행 수를 요약해 줘. 아직 분석은 하지 마.\n\n"
            f"{csv_text(theme)}",
            "같은 내용이 두 번 들어간 행이 있는지 행마다 확인해 줘.",
            "중복을 뺀 값으로 합계를 다시 계산하고, 마지막 합계 행과 맞는지 계산 과정을 보여 줘.",
            f"검증한 수치만 써서 {theme.deliverable}를 요약 - 근거 - 제안 형식으로 작성해 줘. "
            f"메모의 주장('{theme.memo[:30]}…')은 근거가 있는지 따로 표시해 줘."]


def _output(level: str, theme: Theme) -> str:
    n = numbers(theme)
    if level == "하":
        return (f"{theme.deliverable}\n\n총합은 {n.stated:,}{theme.unit}입니다. {theme.memo} "
                "이에 따라 계획대로 진행할 것을 제안합니다.")
    if level == "중":
        return (f"{theme.deliverable}\n\n요약: 총합 {n.stated:,}{theme.unit}, 가장 큰 항목은 "
                f"{n.top}입니다.\n근거: 첨부 CSV의 항목별 값과 합계 행.\n제안: 메모의 방향에 맞춰 "
                "후속 조치를 진행합니다.")
    return (f"{theme.deliverable} (검증본)\n\n요약: {n.label} 행이 두 번 들어가 있어 이를 빼면 실제 "
            f"총합은 {n.actual:,}{theme.unit}이고, 원자료의 합계 {n.stated:,}{theme.unit}는 중복이 "
            f"더해진 값입니다. 가장 큰 항목은 {n.top}입니다.\n근거: 행별 값을 다시 더해 합계 행과 "
            f"대조함. 메모의 주장은 확인 필요: {theme.memo_trap}.\n제안: 원자료 담당자에게 중복 행과 "
            "합계 수정을 요청하고, 확인된 수치로만 보고합니다.")


def _persona(call: FakeCall) -> dict:
    m = re.search(r"응시자 수준: (상|중|하)", call.prompt)
    level = m.group(1) if m else "중"
    theme = theme_of(call.prompt)
    asked = sum(msg.role == "assistant" for msg in call.messages)
    asks = _asks(level, theme, _section(call.prompt, "지시문"))
    if asked < len(asks):
        return {"action": "ask", "content": asks[asked]}
    return {"action": "submit", "content": _output(level, theme)}


def _ai_tool(call: FakeCall) -> str:
    convo = "\n".join(m.content for m in call.messages)
    theme = theme_of(convo)
    n = numbers(theme)
    last = call.messages[-1].content if call.messages else ""
    if "열 구성" in last:
        return (f"CSV는 '{theme.header}' 열로 되어 있고, 항목 행 {len(theme.rows) + 1}개와 "
                "마지막 합계 행이 있습니다.")
    if "두 번 들어간" in last:
        return f"{n.label} 행이 똑같은 값으로 두 번 들어가 있습니다. 나머지 행은 한 번씩입니다."
    if "다시 계산" in last:
        return (f"중복된 {n.label} 행을 빼고 더하면 {n.actual:,}{theme.unit}입니다. 합계 행의 "
                f"{n.stated:,}{theme.unit}는 중복을 포함한 값이라 맞지 않습니다.")
    if "표로" in last:
        lines = "\n".join(f"| {r.split(',')[0]} | {r.split(',')[-1]} |" for r in theme.rows)
        return f"| 항목 | 값 |\n|---|---|\n{lines}\n| 합계 | {n.stated:,} |"
    return (f"요청하신 내용을 정리했습니다. 총합은 {n.stated:,}{theme.unit}이고 가장 큰 항목은 "
            f"{n.top}입니다. {theme.memo}")


# --- 채점 ----------------------------------------------------------------------

_SCORES = {  # mean totals per persona tier (3 = 상, 2 = 중, 1 = 하)
    "pass": {3: 86.0, 2: 61.0, 1: 32.0},
    "flat": {3: 80.0, 2: 72.0, 1: 63.0},  # order kept but gap < 30 → 변별력 부족
    "fail": {3: 58.0, 2: 45.0, 1: 30.0},  # 상 < 70 → 풀이 불가
}
_JITTER = (0.0, 1.5, -1.5)
_RUBRIC_ITEM = re.compile(r"^\d+\. \[(?:과정|산출물)\] .*?\((\d+)점, 역량", re.MULTILINE)
_RATIONALE = {
    3: "원자료와 대조해 중복 행과 합계 오류를 찾아냈고 메모의 주장을 따로 검증했다.",
    2: "형식은 갖췄으나 수치 검증이 부족해 합계 오류를 그대로 옮겼다.",
    1: "AI 출력을 확인 없이 제출해 함정이 반영된 오류와 근거 없는 주장이 남아 있다.",
}


class _DemoState:
    """Per-draft outcome mode and fix count, learned from the prompts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.mode: dict[UUID | None, str] = {}
        self.fixes: dict[UUID | None, int] = defaultdict(int)
        self.gradings: dict[UUID | None, int] = defaultdict(int)

    def observe_generation(self, call: FakeCall) -> None:
        prompt = call.prompt
        with self._lock:
            if FAIL_MARKER in prompt:
                self.mode[call.draft_id] = "fail"
            elif "목표 난이도: 상" in prompt:
                self.mode.setdefault(call.draft_id, "hard")
            if any(f"## 이전 {part}" in prompt for part in ("지시문", "첨부 자료", "채점 기준")):
                self.fixes[call.draft_id] += 1

    def grade(self, call: FakeCall) -> dict:
        log = _section(call.prompt, "제출물: AI 도구 대화 기록")
        turns = log.count("[응시자]")
        tier = 3 if turns >= 3 else 2 if turns == 2 else 1
        with self._lock:
            mode = self.mode.get(call.draft_id, "pass")
            fixes = self.fixes[call.draft_id]
            n = self.gradings[call.draft_id]
            self.gradings[call.draft_id] += 1
        outcome = {"fail": "fail", "hard": "pass" if fixes else "flat"}.get(mode, "pass")
        total = max(0.0, _SCORES[outcome][tier] + _JITTER[n % len(_JITTER)])
        points = [int(p) for p in _RUBRIC_ITEM.findall(call.prompt)] or [30, 20, 30, 20]
        whole = sum(points)
        return {"item_scores": [
            {"item_index": i, "score": round(min(p, total * p / whole), 1),
             "rationale": _RATIONALE[tier]}
            for i, p in enumerate(points)
        ]}


def build_demo_gateway(
    settings: Settings, *, usage_sink: UsageSink | None = None, delay: float = 0.6
) -> FakeGateway:
    """A ``FakeGateway`` answering every workflow call with Korean demo content."""
    gateway = FakeGateway(
        embedding_dim=settings.embedding_dim,
        usage_sink=usage_sink,
        price=settings.prices.get(DEFAULT_GEMINI_MODEL),
    )
    state = _DemoState()
    generation = {CandidateBatch: _candidates, InstructionDraft: _instruction,
                  AttachmentBatch: _attachments, RubricDraft: _rubric}

    def generate(call: FakeCall) -> Any:
        state.observe_generation(call)
        return generation[call.schema](call)

    responders: dict[Role, Callable[[FakeCall], Any]] = {
        Role.GENERATION: generate,
        Role.REVIEW: lambda call: {"consistent": True, "target": None, "issues": []},
        Role.EXAMINEE: _persona,
        Role.AI_TOOL: _ai_tool,
        Role.GRADING: state.grade,
    }
    for role, responder in responders.items():
        gateway.respond_with(role, _slow(responder, delay))
    return gateway


def _slow(responder: Callable[[FakeCall], Any], delay: float) -> Callable[[FakeCall], Any]:
    def respond(call: FakeCall) -> Any:
        if delay:
            time.sleep(delay)
        return responder(call)

    return respond
