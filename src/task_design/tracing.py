"""Langfuse tracing of model calls (spec: 추적).

One Langfuse trace per draft, with trace id ``str(draft_id)``, and one
generation per provider call (schema retries included). Calls without a draft
id each get their own trace. Without Langfuse keys tracing is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

import httpx

from task_design.gateway import Message, Role
from task_design.settings import Settings


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


class Tracer(Protocol):
    def record(self, call: CallRecord) -> None: ...

    def flush(self) -> None: ...


class NoopTracer:
    def record(self, call: CallRecord) -> None:
        pass

    def flush(self) -> None:
        pass


def trace_id_for(draft_id: UUID) -> str:
    """Langfuse trace id of a draft's run."""
    return str(draft_id)


def trace_url(settings: Settings, draft_id: UUID) -> str | None:
    """Link to a draft's trace in the Langfuse UI, or None without ``langfuse_project_id``."""
    if not settings.langfuse_project_id:
        return None
    host = (settings.langfuse_public_host or settings.langfuse_host).rstrip("/")
    return f"{host}/project/{settings.langfuse_project_id}/traces/{trace_id_for(draft_id)}"


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
        self._traced: set[UUID] = set()

    def record(self, call: CallRecord) -> None:
        trace_id = None
        if call.draft_id is not None:
            trace_id = trace_id_for(call.draft_id)
            if call.draft_id not in self._traced:
                self._traced.add(call.draft_id)
                self._langfuse.trace(id=trace_id, name="draft", metadata={"draft_id": trace_id})
        self._langfuse.generation(
            trace_id=trace_id,
            name=call.role.value,
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
            metadata={"provider": call.provider},
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
