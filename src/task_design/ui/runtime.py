"""The process-wide 과제 설계 서비스 behind the Streamlit app.

One service per server process (``st.cache_resource``): its background executor
keeps running drafts when a browser tab closes, and every session polls the same
DB through ``get_draft``.

Environment:
- ``TD_FAKE_GATEWAY=1``: 데모 모드. ``task_design.demo`` answers every model call
  offline with Korean demo content; the seed 역량 체계 and 과제 are loaded if the DB
  has no competencies yet, and stored task embeddings are refreshed if they came
  from another embedder (demo vectors are character n-gram based, so shared wording
  means higher cosine). ``TD_FAKE_DELAY`` (seconds per model call, default 0.6)
  keeps progress watchable. A ``Stepper`` is installed as the run observer:
  the 진행 screen offers 단계별 진행 / 자동 진행 (``TD_DEMO_STEP_MODE=step|auto``,
  default step) and shows how 유사 과제 검색 and 중복 검사 reached their result.
- otherwise the real gateway: ``build_gateway(settings, usage_sink=CostStore(db))``.

Tests call ``use_service`` to run the app script against their own service.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import streamlit as st

from task_design.costs import CostStore
from task_design.db import Database
from task_design.migrate import apply_migrations
from task_design.service import TaskDesignService
from task_design.settings import Settings
from task_design.stepper import Stepper

log = logging.getLogger(__name__)


class StartupError(RuntimeError):
    """The app cannot start; the (Korean) message is shown instead of the screens."""


@dataclass
class AppRuntime:
    service: TaskDesignService
    demo: bool
    stepper: Stepper | None = None  # 데모 모드 단계별 진행 (None in real mode)


_override: AppRuntime | None = None


def use_service(
    service: TaskDesignService | None, *, demo: bool = False, stepper: Stepper | None = None
) -> None:
    """Run the app against ``service`` (tests); ``None`` goes back to the real one.

    ``stepper`` must be the observer ``service`` was built with."""
    global _override
    _override = AppRuntime(service, demo, stepper) if service is not None else None


def get() -> AppRuntime:
    """The runtime for this process. Raises ``StartupError`` with a Korean message."""
    if _override is not None:
        return _override
    return _build()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _background(executor: ThreadPoolExecutor) -> Callable[[Callable[[], None]], None]:
    def run(job: Callable[[], None]) -> None:
        def logged() -> None:
            try:
                job()
            except Exception:  # Workflow stores node errors itself; this is a last resort
                log.exception("background draft job failed")

        executor.submit(logged)

    return run


@st.cache_resource(show_spinner="과제 설계 서비스를 준비하는 중입니다…")
def _build() -> AppRuntime:
    settings = Settings()
    try:
        apply_migrations(settings.database_url)
        db = Database(settings.database_url)
        with db.connection() as conn:
            conn.execute("SELECT 1")
    except Exception as e:
        raise StartupError(
            "데이터베이스에 연결하지 못했습니다. TD_DATABASE_URL과 DB 실행 상태(make db)를 확인하세요."
        ) from e

    demo = _truthy("TD_FAKE_GATEWAY")
    stepper = None
    if demo:
        from task_design.cli import DEFAULT_SEED_DIR
        from task_design.demo import build_demo_gateway
        from task_design.loader import load_dataset
        from task_design.repositories import CompetencyRepository, TaskRepository

        gateway = build_demo_gateway(
            settings, usage_sink=CostStore(db),
            delay=float(os.environ.get("TD_FAKE_DELAY", "0.6")),
        )
        if not CompetencyRepository(db).list():
            load_dataset(DEFAULT_SEED_DIR, db, gateway, settings)
        # Tasks embedded by an older demo embedder (or the real one) are re-embedded.
        refreshed = TaskRepository(db, gateway, settings).refresh_stale_embeddings()
        if refreshed:
            log.warning("re-embedded %d tasks with the demo embedding", refreshed)
        stepper = Stepper(default_mode=settings.demo_step_mode)
    else:
        from task_design.model_gateway import build_gateway

        try:
            gateway = build_gateway(settings, usage_sink=CostStore(db))
        except ValueError as e:  # google-genai: no API key
            raise StartupError(
                "Gemini API 키가 없어 앱을 시작할 수 없습니다. .env 또는 환경 변수에 "
                "TD_GEMINI_API_KEY를 설정하세요. API 키 없이 둘러보려면 TD_FAKE_GATEWAY=1로 "
                "데모 모드를 켜세요."
            ) from e

    # A run paused by the 데모 모드 stepper keeps its worker thread, hence more workers.
    executor = ThreadPoolExecutor(max_workers=32 if demo else 8, thread_name_prefix="draft-run")
    service = TaskDesignService(db, gateway, settings, run_graph=_background(executor),
                                observer=stepper)
    interrupted = service.recover_interrupted_runs()
    if interrupted:
        log.warning("marked %d interrupted draft runs for retry", len(interrupted))
    return AppRuntime(service, demo, stepper)
