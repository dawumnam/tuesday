"""Scripted, recording ``ModelGateway`` for tests.

Usage::

    fake = FakeGateway(embedding_dim=768)
    fake.script(Role.GENERATION, CandidateList(...), {"items": [...]})  # FIFO per role
    fake.respond_with(Role.EXAMINEE, lambda call: "...")  # fallback once queue is empty
    fake.set_embedding("문서 텍스트", [1.0, 0.0, ...])
    fake.script(Role.RERANK, [2, 0, 1])

A scripted response may be: a pydantic instance, a dict (validated against the
call's ``schema``), a str (for schema-less calls), a ``FakeResponse`` (to set
token counts), or a callable ``(FakeCall) -> any of those``. Callables are the
way to script calls that run in parallel (e.g. the three personas): inspect
``call.messages`` and answer accordingly instead of relying on queue order.

Every call is recorded in ``fake.calls``; token usage in ``fake.usage`` and, if
given, forwarded to ``usage_sink`` (same seam the real gateway uses), priced by
``price`` if given.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
import threading
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from task_design.gateway import Message, Role, Usage, UsageSink, Vector
from task_design.settings import ModelPrice

FAKE_PROVIDER = "fake"
FAKE_MODEL = "fake-model"


@dataclass(frozen=True)
class FakeResponse:
    value: Any
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class FakeCall:
    role: Role
    draft_id: UUID | None
    messages: list[Message] = field(default_factory=list)
    schema: type[BaseModel] | None = None
    texts: list[str] = field(default_factory=list)  # embed
    query: str | None = None  # rerank
    docs: list[str] = field(default_factory=list)  # rerank
    result: Any = None

    @property
    def prompt(self) -> str:
        """All message contents joined; handy for `in` checks in responders."""
        return "\n".join(m.content for m in self.messages)


Responder = Callable[[FakeCall], Any]


def _estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


class FakeGateway:
    def __init__(
        self,
        embedding_dim: int = 768,
        usage_sink: UsageSink | None = None,
        price: ModelPrice | None = None,
        embedder: Callable[[str, int], Vector] | None = None,
    ) -> None:
        """``embedder(text, dim)`` embeds texts without a ``set_embedding`` vector
        (default: ``hash_embedding``; ``ngram_embedding`` for 데모 모드 and ``seed --fake``)."""
        self.embedding_dim = embedding_dim
        self.embedder = embedder or hash_embedding
        self.usage_sink = usage_sink
        self.price = price  # prices every call's usage (default: cost 0)
        self.calls: list[FakeCall] = []
        self.usage: list[Usage] = []
        self._queues: dict[Role, deque[Any]] = defaultdict(deque)
        self._defaults: dict[Role, Responder] = {}
        self._embeddings: dict[str, Vector] = {}
        self._lock = threading.RLock()

    # --- scripting ---------------------------------------------------------

    def script(self, role: Role, *responses: Any) -> FakeGateway:
        with self._lock:
            self._queues[role].extend(responses)
        return self

    def respond_with(self, role: Role, responder: Responder) -> FakeGateway:
        """Fallback responder for ``role`` once its scripted queue is empty."""
        self._defaults[role] = responder
        return self

    def set_embedding(self, text: str, vector: Vector) -> FakeGateway:
        if len(vector) != self.embedding_dim:
            raise ValueError(f"vector has {len(vector)} dims, expected {self.embedding_dim}")
        self._embeddings[text] = list(vector)
        return self

    def has_fallback(self, role: Role) -> bool:
        """Whether ``respond_with`` has installed a fallback for ``role``."""
        return role in self._defaults

    def pending(self, role: Role) -> int:
        """Scripted responses not yet consumed for ``role``."""
        return len(self._queues[role])

    # --- inspection --------------------------------------------------------

    def calls_for(self, role: Role) -> list[FakeCall]:
        return [c for c in self.calls if c.role == role]

    def total_tokens(self, draft_id: UUID | None = None) -> int:
        return sum(
            u.input_tokens + u.output_tokens
            for u in self.usage
            if draft_id is None or u.draft_id == draft_id
        )

    # --- ModelGateway ------------------------------------------------------

    def complete(
        self,
        role: Role,
        messages: Sequence[Message],
        schema: type[BaseModel] | None = None,
        draft_id: UUID | None = None,
    ) -> Any:
        call = FakeCall(role=role, draft_id=draft_id, messages=list(messages), schema=schema)
        raw = self._next(role, call)
        input_tokens = output_tokens = None
        if isinstance(raw, FakeResponse):
            raw, input_tokens, output_tokens = raw.value, raw.input_tokens, raw.output_tokens
        result = self._coerce(raw, schema, role)
        out_text = result if isinstance(result, str) else result.model_dump_json()
        self._record(
            call,
            result,
            input_tokens if input_tokens is not None else _estimate_tokens(call.prompt),
            output_tokens if output_tokens is not None else _estimate_tokens(out_text),
        )
        return result

    def embed(self, texts: Sequence[str], draft_id: UUID | None = None) -> list[Vector]:
        call = FakeCall(role=Role.EMBEDDING, draft_id=draft_id, texts=list(texts))
        vectors = [self._embeddings.get(t) or self._default_vector(t) for t in texts]
        self._record(call, vectors, sum(_estimate_tokens(t) for t in texts), 0)
        return vectors

    def rerank(self, query: str, docs: Sequence[str], draft_id: UUID | None = None) -> list[int]:
        call = FakeCall(role=Role.RERANK, draft_id=draft_id, query=query, docs=list(docs))
        with self._lock:
            has_script = bool(self._queues[Role.RERANK]) or Role.RERANK in self._defaults
        order = self._next(Role.RERANK, call) if has_script else list(range(len(docs)))
        if sorted(order) != list(range(len(docs))):
            raise AssertionError(f"scripted rerank order {order} is not a permutation of docs")
        prompt = query + "\n" + "\n".join(docs)
        self._record(call, list(order), _estimate_tokens(prompt), len(docs))
        return list(order)

    # --- internals ---------------------------------------------------------

    def _next(self, role: Role, call: FakeCall) -> Any:
        with self._lock:
            queue = self._queues[role]
            item = queue.popleft() if queue else None
            if item is None:
                if role not in self._defaults:
                    raise AssertionError(
                        f"FakeGateway: no scripted response left for role {role.value!r}"
                    )
                item = self._defaults[role]
        return item(call) if callable(item) and not isinstance(item, type) else item

    @staticmethod
    def _coerce(raw: Any, schema: type[BaseModel] | None, role: Role) -> Any:
        if schema is None:
            if not isinstance(raw, str):
                raise AssertionError(f"schema-less {role.value} call needs a str response")
            return raw
        if isinstance(raw, schema):
            return raw
        if isinstance(raw, BaseModel):
            raw = raw.model_dump()
        return schema.model_validate(raw)

    def _default_vector(self, text: str) -> Vector:
        return self.embedder(text, self.embedding_dim)

    def _record(self, call: FakeCall, result: Any, input_tokens: int, output_tokens: int) -> None:
        call.result = result
        usage = Usage(
            role=call.role,
            draft_id=call.draft_id,
            provider=FAKE_PROVIDER,
            model=FAKE_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=self.price.cost(input_tokens, output_tokens) if self.price else 0.0,
        )
        with self._lock:
            self.calls.append(call)
            self.usage.append(usage)
        if self.usage_sink is not None:
            self.usage_sink.record(usage)


def hash_embedding(text: str, dim: int) -> Vector:
    """Deterministic pseudo-random unit vector per text (unrelated texts ≈ orthogonal).

    Tests rely on this: any two texts are near-orthogonal unless ``set_embedding``
    says otherwise.
    """
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    v = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


_WORD = re.compile(r"[0-9a-z가-힣]+")


def ngram_embedding(text: str, dim: int) -> Vector:
    """Content-based unit vector: a hashed bag of each word's character bigrams and
    trigrams (a one-character word counts as itself), L2-normalised.

    Texts that share wording get a higher cosine, unrelated texts stay near 0, so
    the 데모 모드's vector ranking and 중복 검사 numbers mean something. Deterministic
    and offline. Changing it changes every stored demo embedding (see
    ``TaskRepository.refresh_stale_embeddings``).
    """
    v = [0.0] * dim
    for word in _WORD.findall(text.lower()):
        grams = [word] if len(word) == 1 else [
            word[i:i + n] for n in (2, 3) for i in range(len(word) - n + 1)
        ]
        for gram in grams:
            h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
            v[h % dim] += 1.0 if (h >> 32) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v] if norm else hash_embedding(text, dim)
