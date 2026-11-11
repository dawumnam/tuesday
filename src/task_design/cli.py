"""``task-design`` command line.

Subcommands register themselves in ``_build_parser`` via ``add_parser`` + a handler
``(args, settings, gateway) -> int``.

    task-design seed            # load seed/; embeddings via the real gateway (TD_GEMINI_API_KEY)
    task-design seed --fake     # offline: the demo's character n-gram embeddings
    task-design seed --dir path # another dataset with the same layout (story 50)
    task-design batch examples/batch.json               # 일괄 생성 → drafts in 검토 대기
    task-design batch examples/batch.json --candidate 2 # pick 상황 후보 2 instead of 1
    task-design report                                  # 성공 기준 실측 (last 30 days), Markdown
    task-design report --from 2026-11-01 --to 2026-11-24
    task-design eval-search examples/search_eval.json   # 유사 과제 검색 적중률 (상위 5개)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from task_design.batch import BatchItem, load_combinations, run_batch
from task_design.costs import CostStore
from task_design.db import Database
from task_design.gateway import ModelGateway
from task_design.loader import load_dataset
from task_design.measurement import KST, compute_metrics, period_of_days
from task_design.model_gateway import build_gateway
from task_design.report import render_report
from task_design.repositories import CompetencyRepository, TaskRepository
from task_design.search_eval import STEPS, QueryResult, evaluate, load_queries
from task_design.service import TaskDesignService
from task_design.settings import Settings

# Repository-root ``seed/`` (the package is installed from ``src/``).
DEFAULT_SEED_DIR = Path(__file__).resolve().parents[2] / "seed"
# 12 입력 조합 over the seed 역량 체계 (C1–C8), for `task-design batch`.
EXAMPLE_BATCH_FILE = Path(__file__).resolve().parents[2] / "examples" / "batch.json"
# 12 queries over the seed 과제 with their expected 유사 과제, for `task-design eval-search`.
EXAMPLE_SEARCH_EVAL_FILE = Path(__file__).resolve().parents[2] / "examples" / "search_eval.json"

class CliError(Exception):
    """A user-facing failure: ``main`` prints the (Korean) message and exits 2."""


Handler = Callable[[argparse.Namespace, Settings, ModelGateway | None], int]


def _resolve_gateway(
    args: argparse.Namespace, settings: Settings, db: Database, gateway: ModelGateway | None
) -> ModelGateway:
    if gateway is not None:
        return gateway
    if getattr(args, "fake", False):
        from task_design.testing.fake_gateway import FakeGateway, ngram_embedding

        # Embeddings: the 데모 모드 gateway's content-based ngram vectors; rerank: identity.
        return FakeGateway(embedding_dim=settings.embedding_dim, embedder=ngram_embedding)
    try:
        return build_gateway(settings, usage_sink=CostStore(db))
    except ValueError as e:  # google-genai: no API key in settings or the environment
        raise CliError(
            "Gemini API 키가 없습니다. .env 또는 환경 변수에 TD_GEMINI_API_KEY를 설정하세요. "
            f"({e})"
        ) from e


def _seed(args: argparse.Namespace, settings: Settings, gateway: ModelGateway | None) -> int:
    db = Database(settings.database_url)
    try:
        gw = _resolve_gateway(args, settings, db, gateway)
        try:
            n_competencies, n_tasks = load_dataset(args.dir, db, gw, settings)
        finally:
            tracer = getattr(gw, "tracer", None)
            if tracer is not None:
                tracer.flush()
    finally:
        db.close()
    print(f"역량 {n_competencies}개, 과제 {n_tasks}개를 적재했습니다 ({args.dir}).")
    return 0


def _print_item(total: int) -> Callable[[BatchItem], None]:
    def show(item: BatchItem) -> None:
        d = item.draft
        what = f"{', '.join(item.input.competency_ids)} / {item.input.audience.job} " \
               f"{item.input.audience.career_level} / 난이도 {item.input.difficulty.ko}"
        if item.ok:
            note = f"{len(d.rounds)}회차"
            if d.failure_report is not None:
                fails = ", ".join(f.ko for f in d.failure_report.failure_types)
                note += f", 실패 리포트: {fails}"
            print(f"[{item.index}/{total}] {d.id} {d.state.ko} ({note}) {what}", flush=True)
        else:
            kind = d.error_kind.ko if d.error_kind else "오류"
            print(f"[{item.index}/{total}] {d.id} 실패 ({d.state.ko}, {kind}): {d.error} {what}",
                  flush=True)
    return show


def _batch(args: argparse.Namespace, settings: Settings, gateway: ModelGateway | None) -> int:
    try:
        inputs = load_combinations(args.file)
    except (OSError, ValueError) as e:
        print(f"입력 조합 파일을 읽을 수 없습니다 ({args.file}): {e}", file=sys.stderr)
        return 2
    db = Database(settings.database_url)
    try:
        # Check every combination before running any, so a typo can't stop a batch halfway.
        known = {c.id for c in CompetencyRepository(db).list()}
        unknown = sorted({c for i in inputs for c in i.competency_ids} - known)
        if unknown:
            raise CliError(f"역량 체계에 없는 역량입니다: {', '.join(unknown)} "
                           "(먼저 `task-design seed`로 역량 체계를 적재하세요)")
        gw = _resolve_gateway(args, settings, db, gateway)
        service = TaskDesignService(db, gw, settings)
        authors = [args.author] if args.author else [s.id for s in service.list_staff()]
        print(f"입력 조합 {len(inputs)}개를 일괄 생성합니다 (상황 후보 {args.candidate}번 자동 선택).",
              flush=True)
        try:
            items = run_batch(service, inputs, candidate=args.candidate, author_ids=authors,
                              on_done=_print_item(len(inputs)))
        finally:
            tracer = getattr(gw, "tracer", None)
            if tracer is not None:
                tracer.flush()
    finally:
        db.close()
    ok = sum(item.ok for item in items)
    print(f"완료: {ok}/{len(items)}개 초안이 검토 대기 중입니다. "
          "검토 라벨은 화면에서 사람이 직접 붙입니다 (자동 라벨링 없음).")
    return 0 if ok == len(items) else 1


def _report(args: argparse.Namespace, settings: Settings, gateway: ModelGateway | None) -> int:
    """성공 기준 실측 as Markdown. Reads the DB only: no model calls, no API key."""
    end = args.to or datetime.now(KST).date()
    start = args.start or end - timedelta(days=args.days - 1)
    if start > end:
        raise CliError(f"기간의 시작일({start})이 종료일({end})보다 늦습니다.")
    db = Database(settings.database_url)
    try:
        metrics = compute_metrics(db, settings, period_of_days(start, end))
    finally:
        db.close()
    print(render_report(metrics), end="")
    return 0


def _rank_text(result: QueryResult) -> str:
    return " · ".join(f"{ko} {result.ranks[step] or '—'}" for step, ko in STEPS.items())


def _eval_search(args: argparse.Namespace, settings: Settings,
                 gateway: ModelGateway | None) -> int:
    try:
        queries = load_queries(args.file)
    except (OSError, ValueError) as e:
        print(f"질의 파일을 읽을 수 없습니다 ({args.file}): {e}", file=sys.stderr)
        return 2
    db = Database(settings.database_url)
    try:
        gw = _resolve_gateway(args, settings, db, gateway)
        tasks = TaskRepository(db, gw, settings)
        # A wrong id would silently count as a miss, so check them all first.
        missing = sorted({str(i) for q in queries for i in q.expected if tasks.get(i) is None})
        if missing:
            raise CliError(f"과제 DB에 없는 정답 과제입니다: {', '.join(missing)}")
        try:
            result = evaluate(tasks, CompetencyRepository(db), queries, k=args.k)
        finally:
            tracer = getattr(gw, "tracer", None)
            if tracer is not None:
                tracer.flush()
    finally:
        db.close()
    total = len(result.results)
    print(f"유사 과제 검색 평가: 질의 {total}개, 상위 {result.k}개 기준 (정답 과제의 단계별 순위)")
    for n, r in enumerate(result.results, start=1):
        i = r.query.input
        what = f"{', '.join(i.competency_ids)} / {i.audience.job} {i.audience.career_level}"
        print(f"[{n}/{total}] {'✓' if r.hits['final'] else '✗'} {what} — {_rank_text(r)}")
    rates = " · ".join(
        f"{ko} {result.hit_rate(step) * 100:.0f}% ({result.hit_count(step)}/{total})"
        for step, ko in STEPS.items()
    )
    print(f"적중률 (상위 {result.k}개 안에 정답 과제가 1개 이상): {rates}")
    if args.min_hit_rate is not None and result.hit_rate("final") < args.min_hit_rate:
        print(f"리랭크 후 적중률이 기준 {args.min_hit_rate * 100:.0f}%에 못 미칩니다.",
              file=sys.stderr)
        return 1
    return 0


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"날짜는 YYYY-MM-DD 형식이어야 합니다: {value}") from e


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task-design", description="과제 설계 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="합성 역량 체계와 과제를 DB에 적재 (멱등)")
    seed.add_argument("--dir", type=Path, default=DEFAULT_SEED_DIR, help="데이터셋 디렉터리")
    seed.add_argument(
        "--fake", action="store_true",
        help="FakeGateway 사용 (데모 모드와 같은 글자 n-gram 임베딩, 오프라인)",
    )
    seed.set_defaults(handler=_seed)

    batch = sub.add_parser(
        "batch", help="입력 조합 여러 개를 끝까지 생성해 검토 대기로 남김 (검토 라벨은 사람이)"
    )
    batch.add_argument("file", type=Path, help="입력 조합 JSON 배열 (예: examples/batch.json)")
    batch.add_argument(
        "--candidate", type=int, choices=(1, 2, 3), default=1,
        help="자동으로 고를 상황 후보 번호 (기본 1)",
    )
    batch.add_argument(
        "--author", type=int, default=None,
        help="작성 담당자 id (기본: 담당자를 번갈아 배정해 서로 교차 검토하도록)",
    )
    batch.set_defaults(handler=_batch)

    report = sub.add_parser(
        "report", help="성공 기준 실측(통과 비율, 총 작업 시간)을 마크다운 표로 출력"
    )
    report.add_argument("--days", type=int, default=30,
                        help="종료일까지 며칠을 볼지 (기본 30, --from이 있으면 무시)")
    report.add_argument("--from", dest="start", type=_day, default=None,
                        help="시작일 YYYY-MM-DD (한국 시간, 포함)")
    report.add_argument("--to", type=_day, default=None,
                        help="종료일 YYYY-MM-DD (한국 시간, 포함. 기본: 오늘)")
    report.set_defaults(handler=_report)

    search = sub.add_parser(
        "eval-search", help="질의별 정답 유사 과제로 유사 과제 검색의 적중률을 측정"
    )
    search.add_argument("file", type=Path,
                        help="질의 JSON 배열 (예: examples/search_eval.json)")
    search.add_argument("-k", type=int, default=5, help="상위 몇 개 안에 정답이 있어야 하는지 (기본 5)")
    search.add_argument("--min-hit-rate", type=float, default=None,
                        help="리랭크 후 적중률이 이 값(0~1)보다 낮으면 종료 코드 1")
    search.add_argument(
        "--fake", action="store_true",
        help="FakeGateway 사용 (글자 n-gram 임베딩, 리랭크 없음). `seed --fake`로 적재한 DB용",
    )
    search.set_defaults(handler=_eval_search)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    gateway: ModelGateway | None = None,
) -> int:
    """Entry point. ``settings`` / ``gateway`` are injectable for tests and embedding callers."""
    args = _build_parser().parse_args(argv)
    handler: Handler = args.handler
    try:
        return handler(args, settings or Settings(), gateway)
    except CliError as e:
        print(e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
