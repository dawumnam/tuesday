"""데모 모드 gateway: plausible Korean content, fully offline (``TD_FAKE_GATEWAY=1``).

``build_demo_gateway`` returns a ``FakeGateway`` whose responders read the
workflow's prompts and answer like a model would, so every screen can be clicked
through without an API key:

- 상황 후보: 3 distinct scenarios per call, built for the 대상자's 직무·경력 수준 from
  a pool of templates, skipping anything in the prompt's "피해야 할 상황" list.
- 지시문 / 첨부 자료 (with 함정) / 채점 기준: templates; a rewrite (the prompt carries
  the previous version) comes back visibly revised.
- 응시자 페르소나: 하 asks once and submits, 중 twice, 상 four times while checking the
  attachments. The grader tells the levels apart by the number of 응시자 turns.
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
from typing import Any
from uuid import UUID

from task_design.gateway import Role, UsageSink
from task_design.settings import DEFAULT_GEMINI_MODEL, Settings
from task_design.testing.fake_gateway import FakeCall, FakeGateway
from task_design.workflow.schemas import (
    AttachmentBatch,
    CandidateBatch,
    ExamineeAction,
    GradingDraft,
    InstructionDraft,
    ReviewFindings,
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


# --- 상황 후보 -----------------------------------------------------------------

_SCENARIOS: list[tuple[str, str]] = [
    ("분기 실적 보고 자료 검증",
     "{job} 부서 {career} 담당자인 당신은 내일 오전 임원 회의에 올라갈 분기 실적 요약을 맡았다. "
     "팀장이 지난주 다른 팀원이 정리한 매출 CSV와 메모를 넘기며 'AI로 빨리 정리해 달라'고 했다. "
     "자료에는 지역별 매출과 합계가 들어 있지만, 마감 직전에 급히 취합된 것이라 숫자를 그대로 "
     "믿기 어렵다. 당신은 AI 도구를 활용해 자료를 정리하고 임원이 바로 읽을 요약을 만들어야 한다."),
    ("고객 불만 이메일 대응 초안",
     "{job} 부서 {career} 담당자인 당신에게 주요 거래처의 불만 이메일 세 통이 한꺼번에 전달되었다. "
     "이메일마다 요구 사항과 날짜가 조금씩 다르고, 사내 대응 지침 메모도 함께 받았다. "
     "오늘 안에 일관된 회신 초안을 보내야 하며, AI 도구로 초안을 만들되 사실관계는 직접 "
     "책임져야 한다."),
    ("신규 협력사 비교 검토",
     "{job} 부서 {career} 담당자인 당신은 신규 협력사 후보 세 곳을 비교하는 검토서를 써야 한다. "
     "각 회사가 보낸 제안서 요약과 견적표가 있는데, 단위와 기준일이 서로 다르다. "
     "다음 주 구매 위원회 전에 AI 도구로 비교표와 추천 의견을 정리해야 한다."),
    ("주간 업무 보고 자동화",
     "{job} 부서 {career} 담당자인 당신은 매주 반복되는 주간 업무 보고를 줄이고 싶다. "
     "팀원 다섯 명이 보낸 업무 메모 형식이 제각각이라 취합에 매번 한 시간이 걸린다. "
     "AI 도구로 이번 주 보고서를 만들고, 다음 주에도 쓸 수 있는 요청 문구를 정리해야 한다."),
    ("교육 만족도 설문 분석",
     "{job} 부서 {career} 담당자인 당신은 지난달 사내 교육의 만족도 설문 결과를 분석해야 한다. "
     "응답 CSV에는 점수와 자유 의견이 섞여 있고, 일부 응답은 중복 제출된 것으로 보인다. "
     "교육 담당 임원에게 개선 방향을 담은 한 쪽 보고서를 올려야 한다."),
    ("개인정보가 섞인 자료 정리",
     "{job} 부서 {career} 담당자인 당신은 외부 컨설팅사와 공유할 고객 현황 자료를 준비한다. "
     "원본 자료에는 고객 이름과 연락처가 그대로 들어 있고, 공유 기한은 오늘 오후다. "
     "AI 도구로 요약 자료를 만들되 외부 공유 기준을 지켜야 한다."),
    ("예산 집행 현황 점검",
     "{job} 부서 {career} 담당자인 당신은 상반기 예산 집행 현황을 점검하라는 요청을 받았다. "
     "재무팀이 보낸 집행 내역 CSV와 부서 계획 메모의 항목명이 서로 맞지 않는다. "
     "AI 도구를 활용해 계획 대비 집행률을 정리하고 이상 항목을 찾아 보고해야 한다."),
    ("회의록 기반 실행 계획",
     "{job} 부서 {career} 담당자인 당신은 두 시간짜리 전략 회의의 녹취 요약본을 받았다. "
     "결정 사항과 논의 중인 사항이 섞여 있고 담당자 이름이 일부 빠져 있다. "
     "AI 도구로 실행 계획표를 만들어 참석자들에게 오늘 공유해야 한다."),
    ("경쟁사 동향 브리핑",
     "{job} 부서 {career} 담당자인 당신은 경쟁사 동향 브리핑을 준비한다. "
     "기사 요약 메모 여러 건과 시장 점유율 표가 있는데, 출처와 날짜가 서로 다른 자료가 섞여 있다. "
     "AI 도구로 핵심만 추린 브리핑을 만들되 근거가 약한 주장은 걸러야 한다."),
]


def _candidates(call: FakeCall) -> dict:
    prompt = call.prompt
    job = _field(prompt, "직무", "사업")
    career = _field(prompt, "경력 수준", "3년차")
    avoid = _section(prompt, "피해야 할 상황")
    ids = _IDS.findall(prompt)
    out, variant = [], 0
    while len(out) < 3:
        for i, (title, text) in enumerate(_SCENARIOS):
            suffix = f" (변형 {variant})" if variant else ""
            scenario = text.format(job=job, career=career) + suffix
            if scenario in avoid or any(c["scenario"] == scenario for c in out):
                continue
            refs = [ids[(i + k) % len(ids)] for k in range(min(2, len(ids)))] if ids else []
            out.append({"title": title + suffix, "scenario": scenario,
                        "referenced_task_ids": list(dict.fromkeys(refs))})
            if len(out) == 3:
                break
        variant += 1
    return {"candidates": out}


# --- 지시문 · 첨부 자료 · 채점 기준 ------------------------------------------------------


def _instruction(call: FakeCall) -> dict:
    revised = "## 이전 지시문" in call.prompt
    text = (
        "첨부 자료를 AI 도구로 분석해 의사결정자가 바로 읽을 수 있는 한 쪽 분량의 요약 보고서를 "
        "작성하라. 보고서에는 핵심 수치 3개 이상, 자료에서 발견한 문제점과 그 근거, 다음에 할 일 "
        "제안을 포함한다. 제출물은 AI 도구와의 대화 기록 전체와 최종 보고서이다."
    )
    if revised:
        text += (
            "\n\n작업 순서: ① 첨부 자료의 구조와 항목을 먼저 확인한다. ② 합계·중복 등 수치를 AI "
            "결과와 직접 대조한다. ③ 검증한 수치만 보고서에 쓴다. 보고서 형식은 '요약 - 근거 - "
            "제안' 세 단락으로 한다."
        )
    return {"instruction": text}


def _attachments(call: FakeCall) -> dict:
    revised = "## 이전 첨부 자료" in call.prompt
    csv = ("지역,1월,2월,3월,분기 합계\n서울,1200,1350,1410,3960\n부산,830,790,910,2530\n"
           "대구,640,700,680,2020\n부산,830,790,910,2530\n합계,3500,3630,3910,11040")
    traps = [
        {"location": "5행 (부산)", "description": "부산 행이 한 번 더 들어간 중복 행"},
        {"location": "6행 합계", "description": "합계가 중복 행까지 더한 값이라 실제(3510·…)와 다름"},
    ]
    if revised:
        csv = csv.replace("대구,640,700,680,2020", "대구,640,700,680,2200")
        traps.append({"location": "4행 대구 분기 합계",
                      "description": "월별 합(2020)과 분기 합계(2200)가 맞지 않음"})
    memo = ("팀장 메모: 이번 분기는 부산이 가장 크게 성장했으니 그 점을 강조해 주세요. "
            "지난 분기 대비 전체 매출은 15% 늘었습니다.")
    return {"attachments": [
        {"filename": "분기_매출.csv", "format": "csv", "content": csv, "traps": traps},
        {"filename": "팀장_메모.txt", "format": "memo", "content": memo, "traps": [
            {"location": "첫 문장", "description": "부산은 실제로 가장 크게 성장하지 않았음 (서울이 더 큼)"},
            {"location": "둘째 문장", "description": "전 분기 자료가 없어 15% 증가는 검증할 수 없는 주장"},
        ]},
    ]}


def _rubric(call: FakeCall) -> dict:
    revised = "## 이전 채점 기준" in call.prompt
    m = re.search(r"competency_id는 다음 중 하나입니다: (.+)$", call.prompt, re.MULTILINE)
    ids = [c.strip() for c in m.group(1).split(",")] if m else ["C1"]
    sharper = " 근거 행 번호를 하나 이상 적어야 만점이다." if revised else ""
    items = [
        ("AI 결과 검증 과정", "process", 30,
         "AI가 낸 수치를 확인하지 않고 그대로 사용했다.",
         "일부 수치만 원자료와 대조했거나 함정 1개만 발견했다.",
         "합계·중복 등 함정을 모두 원자료와 대조해 찾아냈다." + sharper),
        ("요청 구체화", "process", 20,
         "지시문을 그대로 붙여넣는 요청만 했다.",
         "형식이나 맥락 중 하나만 지정해 요청했다.",
         "목적·형식·제약을 지정하고 결과를 보며 요청을 고쳤다."),
        ("수치 정확성", "output", 30,
         "보고서 수치에 함정이 반영된 오류가 있다.",
         "오류 일부만 바로잡았다.",
         "모든 핵심 수치가 원자료와 일치한다." + sharper),
        ("보고서 구성과 전달력", "output", 20,
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

_ASKS = {
    "하": ["아래 과제를 해 줘.\n\n(지시문과 첨부 자료를 그대로 붙여넣음)"],
    "중": ["첨부한 매출 CSV와 메모로 임원 보고용 한 쪽 요약을 써 줘. 요약 - 근거 - 제안 형식으로.",
           "핵심 수치를 표로 먼저 보여 주고, 문장을 조금 더 간결하게 고쳐 줘."],
    "상": ["먼저 첨부 CSV의 열 구성과 행 수를 요약해 줘. 아직 분석은 하지 마.",
           "각 지역의 월별 값을 더해서 '분기 합계' 열과 일치하는지 행마다 확인해 줘.",
           "중복된 지역 행이 있는지, 합계 행이 개별 행의 합과 맞는지 계산 과정을 보여 줘.",
           "검증한 수치만 써서 요약 - 근거 - 제안 형식의 한 쪽 보고서를 작성해 줘. "
           "메모의 '부산 최대 성장'과 '15% 증가'는 근거가 있는지 따로 표시해 줘."],
}

_OUTPUTS = {
    "하": "분기 매출 요약 보고서\n\n이번 분기 전체 매출은 11,040으로 크게 늘었습니다. 특히 부산이 "
          "가장 크게 성장했고, 지난 분기 대비 15% 증가했습니다. 앞으로도 부산 중심으로 영업을 "
          "강화하기를 제안합니다.",
    "중": "분기 매출 요약 보고서\n\n요약: 분기 매출은 11,040이며 서울이 가장 큽니다.\n근거: 지역별 "
          "합계 표 참조 (서울 3,960 / 부산 2,530 / 대구 2,020).\n제안: 부산 성장세를 이어가도록 "
          "지원을 늘립니다.",
    "상": "분기 매출 요약 보고서 (검증본)\n\n요약: 중복된 부산 행(5행)을 빼면 실제 분기 매출은 "
          "8,510이며, 원자료의 합계 11,040은 중복이 더해진 값입니다. 성장 폭이 가장 큰 곳은 "
          "서울입니다.\n근거: 행별 월 합계를 다시 계산해 대조함. 메모의 '15% 증가'는 전 분기 "
          "자료가 없어 확인할 수 없음.\n제안: 원자료 담당자에게 중복 행과 합계 수정을 요청하고, "
          "확인된 수치로만 임원 보고를 진행합니다.",
}


def _persona(call: FakeCall) -> dict:
    m = re.search(r"응시자 수준: (상|중|하)", call.prompt)
    level = m.group(1) if m else "중"
    asked = sum(msg.role == "assistant" for msg in call.messages)
    asks = _ASKS[level]
    if asked < len(asks):
        return {"action": "ask", "content": asks[asked]}
    return {"action": "submit", "content": _OUTPUTS[level]}


def _ai_tool(call: FakeCall) -> str:
    last = call.messages[-1].content if call.messages else ""
    if "열 구성" in last:
        return "CSV는 '지역, 1월, 2월, 3월, 분기 합계' 5개 열과 6개 행(합계 행 포함)으로 되어 있습니다."
    if "분기 합계" in last and "확인" in last:
        return "서울 3,960, 부산 2,530, 대구 2,020 모두 월별 합과 분기 합계가 일치합니다."
    if "중복" in last:
        return ("부산 행이 2행과 5행에 똑같이 두 번 있습니다. 중복을 빼면 합계는 8,510이고, "
                "합계 행의 11,040은 중복을 포함한 값입니다.")
    if "표" in last:
        return "| 지역 | 분기 합계 |\n|---|---|\n| 서울 | 3,960 |\n| 부산 | 2,530 |\n| 대구 | 2,020 |"
    return ("요청하신 내용을 정리했습니다. 분기 매출은 11,040이며, 지역별로는 서울이 가장 크고 "
            "부산과 대구가 뒤를 잇습니다. 메모에 따르면 부산의 성장이 두드러집니다.")


# --- 채점 ----------------------------------------------------------------------

_SCORES = {  # (상, 중, 하) mean totals per outcome
    "pass": {3: 86.0, 2: 61.0, 1: 32.0},
    "flat": {3: 80.0, 2: 72.0, 1: 63.0},  # order kept but gap < 30 → 변별력 부족
    "fail": {3: 58.0, 2: 45.0, 1: 30.0},  # 상 < 70 → 풀이 불가
}
_JITTER = (0.0, 1.5, -1.5)
_RUBRIC_ITEM = re.compile(r"^\d+\. \[(?:과정|산출물)\] .*?\((\d+)점, 역량", re.MULTILINE)
_RATIONALE = {
    3: "원자료와 대조해 함정을 찾아냈고 근거를 분명히 적었다.",
    2: "형식은 갖췄으나 수치 검증이 부족해 함정 일부를 놓쳤다.",
    1: "AI 출력을 그대로 제출해 함정이 반영된 오류가 남아 있다.",
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


# Keep the schema imports referenced (documentation of what each role answers).
_ = (ExamineeAction, GradingDraft, ReviewFindings)
