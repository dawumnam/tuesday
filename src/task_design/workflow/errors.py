"""Why a workflow run failed: the ``DraftErrorKind`` stored next to ``Draft.error``."""

from __future__ import annotations

import httpx
from google.genai import errors as genai_errors
from pydantic import ValidationError

from task_design.domain import DraftErrorKind
from task_design.model_gateway import InvalidModelOutput, ProviderNotConfigured


class WorkflowError(Exception):
    """A workflow step failed for a known reason; ``kind`` is stored on the draft.

    Messages are Korean (the raw text is shown under 오류 상세).
    """

    kind: DraftErrorKind = DraftErrorKind.UNEXPECTED


class CandidatesExhausted(WorkflowError):
    """Every 상황 후보 generation attempt was used up without 3 acceptable candidates
    (each new, not already shown, and below the duplicate threshold)."""

    kind = DraftErrorKind.CANDIDATES_EXHAUSTED


class InvalidModelContent(WorkflowError):
    """The model kept producing schema-valid output that breaks a domain rule."""

    kind = DraftErrorKind.INVALID_MODEL_OUTPUT


_UNAVAILABLE = (httpx.HTTPError, genai_errors.APIError, TimeoutError, ConnectionError)


def error_kind_of(exc: BaseException) -> DraftErrorKind:
    if isinstance(exc, WorkflowError):
        return exc.kind
    if isinstance(exc, InvalidModelOutput | ValidationError):
        return DraftErrorKind.INVALID_MODEL_OUTPUT
    if isinstance(exc, _UNAVAILABLE):
        return DraftErrorKind.MODEL_UNAVAILABLE
    if isinstance(exc, ProviderNotConfigured):
        return DraftErrorKind.CONFIGURATION
    return DraftErrorKind.UNEXPECTED
