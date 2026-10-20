"""``task-design`` command line.

Subcommands register themselves in ``_build_parser`` via ``add_parser`` + a handler
``(args, settings, gateway) -> int``.

    task-design seed            # load seed/; embeddings via the real gateway (TD_GEMINI_API_KEY)
    task-design seed --fake     # offline: deterministic hash-based embeddings
    task-design seed --dir path # another dataset with the same layout (story 50)
    task-design batch examples/batch.json               # 일괄 생성 → drafts in 검토 대기
    task-design batch examples/batch.json --candidate 2 # pick 상황 후보 2 instead of 1
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from task_design.batch import BatchItem, load_combinations, run_batch
from task_design.costs import CostStore
from task_design.db import Database
from task_design.gateway import ModelGateway
from task_design.loader import load_dataset
from task_design.model_gateway import build_gateway
from task_design.service import TaskDesignService, UnknownCompetency
from task_design.settings import Settings

# Repository-root ``seed/`` (the package is installed from ``src/``).
DEFAULT_SEED_DIR = Path(__file__).resolve().parents[2] / "seed"
# 12 입력 조합 over the seed 역량 체계 (C1–C8), for `task-design batch`.
EXAMPLE_BATCH_FILE = Path(__file__).resolve().parents[2] / "examples" / "batch.json"

Handler = Callable[[argparse.Namespace, Settings, ModelGateway | None], int]


def _resolve_gateway(
    args: argparse.Namespace, settings: Settings, db: Database, gateway: ModelGateway | None
) -> ModelGateway:
    if gateway is not None:
        return gateway
    if getattr(args, "fake", False):
        from task_design.testing.fake_gateway import FakeGateway

        # Embeddings: deterministic per-text unit vectors (sha256-seeded); rerank: identity.
        return FakeGateway(embedding_dim=settings.embedding_dim)
    return build_gateway(settings, usage_sink=CostStore(db))


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
            print(f"[{item.index}/{total}] {d.id} 실패 ({d.state.ko}): {d.error} {what}",
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
        gw = _resolve_gateway(args, settings, db, gateway)
        service = TaskDesignService(db, gw, settings)
        authors = [args.author] if args.author else [s.id for s in service.list_staff()]
        print(f"입력 조합 {len(inputs)}개를 일괄 생성합니다 (상황 후보 {args.candidate}번 자동 선택).",
              flush=True)
        try:
            items = run_batch(service, inputs, candidate=args.candidate, author_ids=authors,
                              on_done=_print_item(len(inputs)))
        except UnknownCompetency as e:
            print(f"{e} (먼저 `task-design seed`로 역량 체계를 적재하세요)", file=sys.stderr)
            return 2
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task-design", description="과제 설계 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="합성 역량 체계와 과제를 DB에 적재 (멱등)")
    seed.add_argument("--dir", type=Path, default=DEFAULT_SEED_DIR, help="데이터셋 디렉터리")
    seed.add_argument(
        "--fake", action="store_true", help="FakeGateway 사용 (해시 기반 결정적 임베딩, 오프라인)"
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
    return handler(args, settings or Settings(), gateway)


if __name__ == "__main__":
    sys.exit(main())
