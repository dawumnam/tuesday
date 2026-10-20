"""Langfuse tracing of model calls (spec: 추적).

One Langfuse trace per graph execution (start, regenerate, select, retry), named
after it; a draft's traces form one Langfuse session (session id = draft id).
Each provider call (schema retries included) is a generation named after the
workflow node that made it. The workflow marks executions and nodes with
``run_scope`` / ``node_scope`` (context variables, so parallel nodes keep their
own); the gateway reads them via ``current_scope``. A call with a draft id
outside any execution (e.g. the 확정 embedding) gets a trace of its own in the
draft's session; calls without a draft id get their own trace. Without Langfuse
keys tracing is a no-op.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

import httpx

from task_design.gateway import Message, Role
from task_design.settings import Settings


@dataclass(frozen=True)
class Scope:
    """Where a model call happens: which graph execution and which node."""

    trace_id: str | None = None  # the graph execution's trace (None: outside one)
    run: str | None = None  # execution name: start / regenerate / select / retry
    node: str | None = None  # workflow node making the call


_scope: ContextVar[Scope] = ContextVar("task_design_trace_scope", default=Scope())


@contextmanager
def run_scope(run: str) -> Iterator[Scope]:
    """One graph execution: a new trace id for every model call made inside."""
    token = _scope.set(Scope(trace_id=str(uuid.uuid4()), run=run))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)


@contextmanager
def node_scope(node: str) -> Iterator[None]:
    token = _scope.set(Scope(trace_id=_scope.get().trace_id, run=_scope.get().run, node=node))
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> Scope:
    return _scope.get()


@dataclass(frozen=True)
class CallRecord:
    role: Role
    draft_id: UUID | None
    provider: str
    model: str
    input: Sequence[Message] | Sequence[str]  # messages, or texts for embeddings
    output: str | None
    start: datetime
    end: datetime
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    parameters: dict[str, str | int | None] | None = None
    scope: Scope = Scope()


class Tracer(Protocol):
    def record(self, call: CallRecord) -> None: ...

    def flush(self) -> None: ...


class NoopTracer:
    def record(self, call: CallRecord) -> None:
        pass

    def flush(self) -> None:
        pass


def session_id_for(draft_id: UUID) -> str:
    """Langfuse session id of a draft (its traces: one per graph execution)."""
    return str(draft_id)


def trace_url(settings: Settings, draft_id: UUID) -> str | None:
    """Link to a draft's session (all its traces) in the Langfuse UI, or None without
    ``langfuse_project_id``."""
    if not settings.langfuse_project_id:
        return None
    host = (settings.langfuse_public_host or settings.langfuse_host).rstrip("/")
    return (f"{host}/project/{settings.langfuse_project_id}/sessions/"
            f"{session_id_for(draft_id)}")


class LangfuseTracer:
    def __init__(
        self,
        public_key: str,
        secret_key: str,
        host: str,
        httpx_client: httpx.Client | None = None,
    ) -> None:
        from langfuse import Langfuse

        self._langfuse = Langfuse(
            public_key=public_key, secret_key=secret_key, host=host, httpx_client=httpx_client
        )
        self._traced: set[str] = set()
        self._lock = threading.Lock()

    def record(self, call: CallRecord) -> None:
        scope = call.scope
        trace_id = scope.trace_id or str(uuid.uuid4())
        with self._lock:
            new = trace_id not in self._traced
            self._traced.add(trace_id)
        if new:
            session = session_id_for(call.draft_id) if call.draft_id is not None else None
            self._langfuse.trace(
                id=trace_id, name=scope.run or call.role.value, session_id=session,
                metadata={"draft_id": session} if session else None,
            )
        self._langfuse.generation(
            trace_id=trace_id,
            name=scope.node or call.role.value,
            model=call.model,
            model_parameters=call.parameters,
            input=[
                m.model_dump() if isinstance(m, Message) else m for m in call.input
            ],
            output=call.output,
            start_time=call.start,
            end_time=call.end,
            # `usage` (not usage_details/cost_details): understood by the v2 server in compose.
            usage={
                "input": call.input_tokens,
                "output": call.output_tokens,
                "unit": "TOKENS",
                "total_cost": call.cost_usd,
            },
            metadata={"provider": call.provider, "role": call.role.value},
            level="ERROR" if call.error else "DEFAULT",
            status_message=call.error,
        )

    def flush(self) -> None:
        self._langfuse.flush()


def make_tracer(settings: Settings) -> Tracer:
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return NoopTracer()
    return LangfuseTracer(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
