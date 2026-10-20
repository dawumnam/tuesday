"""``task-design`` command line.

Subcommands register themselves in ``_build_parser`` via ``add_parser`` + a handler
``(args, settings, gateway) -> int``. Ticket 11 adds ``batch`` the same way.

    task-design seed            # load seed/; embeddings via the real gateway (TD_GEMINI_API_KEY)
    task-design seed --fake     # offline: deterministic hash-based embeddings
    task-design seed --dir path # another dataset with the same layout (story 50)
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from task_design.costs import CostStore
from task_design.db import Database
from task_design.gateway import ModelGateway
from task_design.loader import load_dataset
from task_design.model_gateway import build_gateway
from task_design.settings import Settings

# Repository-root ``seed/`` (the package is installed from ``src/``).
DEFAULT_SEED_DIR = Path(__file__).resolve().parents[2] / "seed"

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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="task-design", description="과제 설계 에이전트")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="합성 역량 체계와 과제를 DB에 적재 (멱등)")
    seed.add_argument("--dir", type=Path, default=DEFAULT_SEED_DIR, help="데이터셋 디렉터리")
    seed.add_argument(
        "--fake", action="store_true", help="FakeGateway 사용 (해시 기반 결정적 임베딩, 오프라인)"
    )
    seed.set_defaults(handler=_seed)
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
