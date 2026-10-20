# 과제 설계 에이전트 (MVP)

AI 활용 역량 평가용 과제(상황·지시문·첨부 자료·채점 기준)를 생성하고, 상·중·하 모의 응시자로 검증·자기 수정한 뒤 담당자 검토로 넘기는 에이전트입니다. 설계는 `docs/specs/0001-task-design-agent-mvp.md`, 용어는 `GLOSSARY.md`, 결정 기록은 `docs/adr/`에 있습니다.

모든 모델 호출은 Gemini(`gemini-3.8-flash`, 임베딩 `gemini-embedding-2`)를 씁니다.

## 구성

| 서비스 | 주소 | 설명 |
|---|---|---|
| app | http://localhost:8501 | Streamlit 화면. 시작할 때 마이그레이션을 적용하고, 원하면 시드를 적재합니다. |
| db | `localhost:54329` | Postgres 17 + pgvector, pg_trgm (user/password/db 모두 `task_design`) |
| langfuse | http://localhost:3300 | 추적 도구. 로그인 `admin@example.com` / `admin1234` |

## 준비물

- Docker (Compose v2.24 이상)
- [uv](https://docs.astral.sh/uv/) 와 Python 3.13 (호스트에서 CLI·테스트를 돌릴 때)
- Gemini API 키: https://aistudio.google.com/apikey 에서 발급합니다. 유료 등급 키를 권장합니다(일괄 생성은 호출 수가 많습니다).

## 빠른 시작 (docker compose)

1. 저장소 루트에 `.env`를 만들고 API 키를 넣습니다. `.env`는 git에 올라가지 않습니다.

   ```sh
   TD_GEMINI_API_KEY=발급받은-키
   TD_SEED_ON_START=true   # 첫 실행 때 역량 체계와 과제 30개를 적재 (임베딩 호출 30회)
   ```

2. 전체를 띄웁니다.

   ```sh
   docker compose up -d --build --wait   # 또는 make up
   ```

   app 컨테이너는 시작할 때마다 `python -m task_design.migrate`를 실행하고, `TD_SEED_ON_START=true`이면 `task-design seed`를 실행한 뒤 Streamlit을 띄웁니다. 시드는 멱등이라 다시 돌려도 같은 결과지만 매번 임베딩 비용이 들기 때문에, 첫 적재가 끝나면 `false`로 돌려 두세요. 시드가 실패해도(예: 키 없음) 화면은 뜨고 로그에 경고만 남습니다.

3. http://localhost:8501 을 엽니다. 상단에서 담당자(김지현/박서준)를 고릅니다.

종료는 `docker compose down`, 데이터까지 지우려면 `docker compose down -v`입니다.

## 환경 변수

모든 설정은 `TD_` 접두사 환경 변수(또는 `.env`)로 바꿀 수 있습니다. 중첩 필드는 `__`로 잇습니다. 정의는 `src/task_design/settings.py`에 있습니다. compose의 app 컨테이너는 `.env`의 `TD_*` 값을 그대로 받되, DB와 Langfuse 주소는 compose 내부 주소로 고정됩니다.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `TD_GEMINI_API_KEY` | (없음) | Gemini API 키. 없으면 `GEMINI_API_KEY` / `GOOGLE_API_KEY`를 씁니다. |
| `TD_DATABASE_URL` | `postgresql://task_design:task_design@localhost:54329/task_design` | 앱 DB |
| `TD_SEED_ON_START` | `false` | app 컨테이너 시작 시 시드 적재 여부 (compose 전용) |
| `TD_LANGFUSE_HOST` / `TD_LANGFUSE_PUBLIC_KEY` / `TD_LANGFUSE_SECRET_KEY` | `http://localhost:3300` / 없음 / 없음 | 두 키가 모두 있어야 추적합니다. 로컬 Langfuse의 키는 `pk-lf-local` / `sk-lf-local`입니다. |
| `TD_LANGFUSE_PROJECT_ID` | 없음 | 지표 화면의 초안별 trace 링크용. 로컬 Langfuse 프로젝트 id는 `task-design`입니다. |
| `TD_LANGFUSE_PUBLIC_HOST` | `TD_LANGFUSE_HOST` | 브라우저에서 여는 trace 링크의 Langfuse 주소. compose에서는 앱이 내부 주소(`http://langfuse:3000`)로 접속하므로 `http://localhost:3300`으로 설정되어 있습니다. |
| `TD_FAKE_GATEWAY` | 없음 | `1`이면 데모 모드: 모델 호출 없이 한국어 예시 응답으로 화면 전체를 눌러 볼 수 있습니다. 역량 체계가 비어 있으면 시드를 자동으로 적재합니다. |
| `TD_FAKE_DELAY` | `0.6` | 데모 모드에서 모델 호출 1회당 지연(초). 진행 화면을 보기 좋게 합니다. |
| `TD_PASS_RATIO_TARGET` / `TD_WORK_MINUTES_TARGET` | `0.70` / `42` | 대시보드 성공 기준 (전면 재작성 없이 통과한 비율, 과제당 작업 시간 분) |
| `TD_SOLVABLE_MIN_SCORE` | `70` | 풀이 가능성: 상 페르소나 평균 점수 하한 |
| `TD_DISCRIMINATION_MIN_GAP` | `30` | 변별력: 상 − 하 점수 차 하한 |
| `TD_GRADING_MAX_STDEV` | `5` | 채점 일관성: 3회 채점 표준편차 상한 |
| `TD_DUPLICATE_THRESHOLD` | `0.85` | 상황 후보 중복 판정 코사인 유사도 |
| `TD_MAX_ROUNDS` | `3` | 회차 상한 (1회차 포함) |
| `TD_DRAFT_TOKEN_BUDGET` | `2000000` | 초안당 토큰 예산. 넘으면 자기 수정을 멈추고 실패 리포트와 함께 검토 대기로 보냅니다. |
| `TD_ROUTING__<역할>__MODEL` / `__THINKING` | `gemini-3.8-flash` / `high` | 역할별 모델과 사고 수준. 역할: `GENERATION`, `REVIEW`, `EXAMINEE`, `AI_TOOL`, `GRADING`, `RERANK`, `EMBEDDING` |
| `TD_PRICES` | 코드의 단가표 | 비용 추정 단가(JSON, 통째로 교체). `gemini-3.8-flash` 단가는 2027-01-01에 두 배가 됩니다. |

## 호스트에서 실행 (개발)

```sh
uv sync
make infra              # Postgres + Langfuse만 띄움
make migrate            # 마이그레이션
make seed               # 시드 적재 (실제 임베딩, API 키 필요)
uv run streamlit run src/task_design/ui/app.py
```

API 키 없이 둘러보기(데모 모드): `make db && TD_FAKE_GATEWAY=1 uv run streamlit run src/task_design/ui/app.py`.
데모 모드에서는 난이도 '상'을 고르면 자기 수정 루프 1회가, 메모에 '실패 시연'이라고 적으면 실패 리포트가 나옵니다.
진행 중인 실행은 서버 프로세스에서 돌기 때문에 브라우저를 닫아도 계속되며, '초안 목록'이나 `?draft=<id>` 주소로 다시 열 수 있습니다. 서버를 다시 시작하면 중단된 실행에 오류가 표시되고 '재시도'로 이어서 진행합니다.

추적을 보려면 호스트 `.env`에 `TD_LANGFUSE_PUBLIC_KEY=pk-lf-local`, `TD_LANGFUSE_SECRET_KEY=sk-lf-local`, `TD_LANGFUSE_PROJECT_ID=task-design`도 넣습니다.

## 일괄 생성

```sh
task-design batch examples/batch.json                 # 상황 후보 1번을 자동 선택
task-design batch examples/batch.json --candidate 2   # 2번을 선택
task-design batch my.json --author 1                  # 작성자를 1번 담당자로 고정
```

- 입력 조합 파일은 `DraftInput` JSON 배열입니다. `examples/batch.json`에 시드 역량 C1–C8을 쓰는 조합 12개가 있습니다.

  ```json
  [{"competency_ids": ["C4"], "audience": {"job": "재무·회계", "career_level": "3년차"},
    "difficulty": "medium", "memo": "CSV 첨부 포함"}]
  ```

- 조합마다 생성 → 상황 후보 자동 선택 → 모의 응시·판정·자기 수정까지 끝까지 돌리고, 초안을 **검토 대기**로 남깁니다. 작성자는 기본으로 두 담당자를 번갈아 배정하므로, 각자 상대의 초안을 검토하면 됩니다.
- **검토 라벨은 자동으로 붙이지 않습니다** (ADR 0002). 성공 기준이 "사람이 다시 쓰지 않아도 되는가"이기 때문에, 라벨은 반드시 사람이 화면에서 붙입니다.
- 실패한 조합이 있어도 나머지는 계속 돌고, 마지막에 요약을 보여 줍니다. 모든 초안이 검토 대기에 도달하면 종료 코드 0, 하나라도 실패하면 1, 입력 파일·역량·API 키 문제면 2입니다. 역량은 실행 전에 한꺼번에 확인하므로, 없는 역량이 있으면 아무 초안도 만들지 않고 멈춥니다.
- 비용·시간: 초안 하나가 최대 3회차 × (페르소나 3 + 채점 9 + 수정 1)번가량 호출합니다. 12개 조합이면 수십 분이 걸릴 수 있습니다. 모든 호출의 토큰과 추정 비용은 `model_costs` 테이블과 Langfuse(초안 id = trace id)에 남습니다.
- compose 환경에서는 `docker compose exec app task-design batch examples/batch.json`으로 실행합니다.

## 시연 런북

1. `.env`에 `TD_GEMINI_API_KEY`와 `TD_SEED_ON_START=true`를 넣고 `docker compose up -d --build --wait`.
2. `docker compose logs app`에서 `역량 8개, 과제 30개를 적재했습니다`를 확인한 뒤 `.env`의 `TD_SEED_ON_START`를 `false`로 바꿉니다.
3. 일괄 생성으로 지표를 채웁니다: `docker compose exec app task-design batch examples/batch.json`. 각 줄에 초안 id, 상태, 회차 수, 실패 리포트 유무가 나옵니다.
4. 화면에서 담당자를 바꿔 가며 상대가 작성한 초안을 검토합니다. 상황·지시문·채점 기준마다 검토 라벨(유지 / 부분 수정 / 전면 재작성)을 붙이고 확정 또는 반려합니다.
5. 실시간 흐름 시연: 화면에서 새 초안을 입력 → 상황 후보 3개 중 선택 → 진행 화면에서 단계·페르소나 상태 확인 → 검토.
6. 대시보드에서 기간별 지표(전면 재작성 없이 통과한 비율, 작업 시간, 비용)를 확인합니다.
7. Langfuse(http://localhost:3300)에서 초안 id로 trace를 열어 역할별 호출, 토큰, 비용을 봅니다.
8. 확정한 과제가 다음 초안의 유사 과제 검색에 나오는지 확인합니다.

## 테스트

```sh
make test   # compose의 Postgres(54329)를 띄우고 uv run pytest
```

테스트는 세션마다 새 DB(`td_test_<hex>`)를 만들어 쓰고 끝나면 지웁니다. 모델 호출은 모두 `FakeGateway`로 바꾸므로 API 키가 필요 없고 네트워크도 쓰지 않습니다.
