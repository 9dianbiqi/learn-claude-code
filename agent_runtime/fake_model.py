from __future__ import annotations

import copy
from collections.abc import Iterable

from .models import ModelResponse


class ScriptedModel:
    """Deterministic model adapter used by tests and the local eval suite."""

    name = "scripted-fake"

    def __init__(self, responses: Iterable[ModelResponse]):
        self.responses = list(responses)
        self.calls: list[list[dict]] = []
        self.tool_schemas: list[list[dict]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        self.calls.append(copy.deepcopy(messages))
        self.tool_schemas.append(copy.deepcopy(tools))
        if not self.responses:
            raise RuntimeError("ScriptedModel has no response left")
        return copy.deepcopy(self.responses.pop(0))
