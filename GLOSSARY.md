# 과제 설계 에이전트

에이블제이 AI 사용능력 평가 과제를 생성·검증해 담당자의 과제 설계 시간을 줄이는 시스템의 도메인 언어.

## 사람

**담당자**:
평가 서비스 기획·개발 담당자로, 한 초안에 대해 작성자 또는 검토자 역할을 맡는다. 작성자는 자기 초안의 최종 검토를 할 수 없다.
_Avoid_: 사용자, 운영자, 관리자

## 평가 구성

**역량 체계**:
에이블제이가 평가하는 AI 사용능력을 역량 → 하위 요소 → 상·중·하 수준 지표로 구조화한 기준.
_Avoid_: 역량 프레임워크, 평가 항목

**수준 지표**:
특정 역량의 상·중·하 수준에서 관찰되는 구체적 행동 서술.
_Avoid_: 레벨 기준, 등급 설명

**대상자**:
과제를 응시할 집단(직무·경력 수준)으로, 과제 상황의 맥락을 결정한다.
_Avoid_: 타깃, 수험자 그룹

**과제**:
상황 + 지시문 + 첨부 자료(선택) + 채점 기준으로 이루어진 평가 단위.
_Avoid_: 문항, 문제

**상황**:
대상자가 업무 중 마주칠 법한 맥락과 목표를 서술한 과제의 도입부.
_Avoid_: 시나리오, 배경

**지시문**:
응시자가 수행해야 할 작업과 제출물을 명시한 문장.
_Avoid_: 문제문, 요구사항

**채점 기준**:
제출물을 평가하는 항목별 배점과 판정 기준(루브릭).
_Avoid_: 루브릭(동의어로만 허용), 정답, 평가표

**과정 항목**:
제출물 중 AI 사용 과정(대화 기록)을 판정하는 채점 기준 항목. 모든 과제에 1개 이상 있다.
_Avoid_: 프로세스 점수

**산출물 항목**:
제출물 중 최종 산출물의 품질을 판정하는 채점 기준 항목.
_Avoid_: 결과 점수

**함정**:
응시자의 검증 행동을 드러내려고 첨부 자료에 의도적으로 심어 둔 오류나 누락.
_Avoid_: 트랩, 오답 유도

**제출물**:
응시자가 제출하는 AI 사용 과정(대화 기록)과 최종 산출물의 묶음.
_Avoid_: 답안, 응답

## 생성·검증

**초안**:
에이전트가 생성해 담당자 검토 전 상태인 과제.
_Avoid_: 드래프트, 생성물

**상황 후보**:
상황 설계 단계에서 담당자 선택을 위해 제시되는 상황 3개.
_Avoid_: 옵션, 시안

**모의 응시**:
응시자 페르소나가 AI 도구 LLM을 사용해 과제를 풀고 채점 에이전트가 채점하는 검증 절차.
_Avoid_: 시뮬레이션, 테스트 풀이

**응시자 페르소나**:
수준 지표를 주입받아 특정 수준(상·중·하)의 AI 사용 행동을 흉내 내는 사람 역할 에이전트. 수준은 모델 성능이 아니라 행동으로 구분한다.
_Avoid_: 가상 응시자, 봇 응시자

**AI 도구 LLM**:
모의 응시에서 응시자 페르소나가 사용하는 AI 도구 역할의 LLM으로, 모든 수준에서 동일하다.
_Avoid_: 어시스턴트, 챗봇

**풀이 가능성**:
상 수준 응시자 페르소나가 지시문만으로 채점 기준을 충족하는 제출물을 만들 수 있는지 여부.
_Avoid_: 난이도, 해결 가능성

**변별력**:
응시자 페르소나의 점수가 상 > 중 > 하 순서를 지키고 상·하 점수 차가 기준값 이상인 성질.
_Avoid_: 난이도 분산

**채점 일관성**:
같은 제출물을 반복 채점했을 때 점수 편차가 임계값 이하인 성질.
_Avoid_: 신뢰도(통계 용어와 혼동)

**자기 수정 루프**:
검증 실패 유형에 따라 지시문·채점 기준·첨부 자료 중 해당 부분만 다시 생성하는 반복(최대 3회). 상황은 수정 대상이 아니다.
_Avoid_: 재시도, 리트라이

**실패 리포트**:
자기 수정 루프를 다 쓰고도 검증에 실패한 초안에 붙여 담당자에게 넘기는 실패 원인 요약.
_Avoid_: 에러 로그

## 검토·측정

**최종 검토**:
다른 담당자가 초안을 확정 과제로 승인하는 교차 검토.
_Avoid_: 검수, QA

**확정 과제**:
최종 검토를 통과해 과제 DB에 들어가고 이후 유사 과제 검색 대상이 되는 과제.
_Avoid_: 완료 과제, 승인본

**반려**:
최종 검토에서 확정되지 않은 초안의 결말로, 검토 라벨과 함께 보존된다.
_Avoid_: 폐기, 거절

**검토 라벨**:
최종 검토 시 상황·지시문·채점 기준 각각에 붙이는 유지 / 부분 수정 / 전면 재작성 판정.
_Avoid_: 수정 등급

**전면 재작성**:
상황·지시문·채점 기준 중 하나라도 처음부터 다시 쓴 경우로, 해당 초안은 성공으로 세지 않는다.
_Avoid_: 대폭 수정, 리라이트

**총 작업 시간**:
과제 1개에 대해 사람 시간(입력·상황 선택·검토 편집, 비활성 제외)과 에이전트 대기 시간(생성·검증)을 합한 시간.
_Avoid_: 소요 시간, 리드타임

## Code names

코드 식별자는 영어, 과제 콘텐츠는 한국어. 아래 대응을 따른다(타입은 `task_design.domain`, 역할은 `task_design.gateway.Role`).

| 용어 | 식별자 |
|---|---|
| 담당자 | `Staff` |
| 작성자 / 검토자 | `author_id` / `reviewer_id` |
| 역량 체계 / 역량 | `competency` (`competency_id`) |
| 수준 지표 | `level indicator` |
| 상·중·하 | `Level.HIGH` / `Level.MEDIUM` / `Level.LOW` |
| 대상자 | `Audience` (`job`, `career_level`) |
| 과제 | `Task` |
| 확정 과제 | `Task` in the task DB (`TaskRepository`) |
| 상황 | `scenario` (`Component.SCENARIO`) |
| 지시문 | `instruction` (`Component.INSTRUCTION`) |
| 첨부 자료 | `Attachment` (`Component.ATTACHMENTS`) |
| 함정 | `Trap` |
| 채점 기준 | `Rubric` (`Component.RUBRIC`) |
| 채점 기준 항목 | `RubricItem` |
| 과정 항목 / 산출물 항목 | `RubricItemType.PROCESS` / `RubricItemType.OUTPUT` |
| 제출물 | submission = `MockExamResult.transcript` + `MockExamResult.output` |
| 초안 | `Draft` |
| 초안 상태 | `DraftState` (생성 중 `GENERATING`, 상황 선택 대기 `AWAITING_SCENARIO_SELECTION`, 검증 중 `VERIFYING`, 검토 대기 `AWAITING_REVIEW`, 확정 `CONFIRMED`, 반려 `REJECTED`) |
| 초안 회차 | `DraftRound` |
| 상황 후보 | `ScenarioCandidate` |
| 모의 응시 / 모의 응시 결과 | mock exam / `MockExamResult` |
| 응시자 페르소나 | examinee persona (`Role.EXAMINEE`) |
| AI 도구 LLM | `Role.AI_TOOL` |
| 채점 에이전트 / 채점 1회 | `Role.GRADING` / `Grading` |
| 검토 에이전트 | `Role.REVIEW` |
| 판정 | `Verdict` |
| 풀이 가능성 / 변별력 / 채점 일관성 | `solvable` / `discriminating` / `consistent` |
| 풀이 불가 / 변별력 부족 / 채점 불일치 | `FailureType.UNSOLVABLE` / `LOW_DISCRIMINATION` / `INCONSISTENT_GRADING` |
| 자기 수정 루프 | self-correction loop |
| 실패 리포트 | `FailureReport` |
| 최종 검토 | `TaskDesignService.review` / `Review` |
| 검토 라벨 | `ReviewLabel` (유지 `KEEP`, 부분 수정 `PARTIAL_EDIT`, 전면 재작성 `FULL_REWRITE`) |
| 확정 / 반려 (결정) | `ReviewDecision.CONFIRM` / `ReviewDecision.REJECT` |
| 측정 이벤트 | `MeasurementEvent` (`IntervalKind.HUMAN_ACTIVE` / `AGENT_RUN`) |
| 총 작업 시간 | total work time (`Metrics.mean_total_work_minutes`) |
| 비용 | `Cost`, per call `gateway.Usage` |
| 과제 설계 서비스 | `TaskDesignService` |
| 모델 게이트웨이 | `ModelGateway` |
