from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    def as_content(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


@dataclass
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.stop_reason is None:
            self.stop_reason = "tool_use" if self.tool_calls else "end_turn"

    @property
    def content(self) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if self.text:
            blocks.append({"type": "text", "text": self.text})
        blocks.extend(call.as_content() for call in self.tool_calls)
        return blocks

    def as_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "stop_reason": self.stop_reason,
            "usage": self.usage,
            "raw": self.raw,
        }


@dataclass(frozen=True)
class RunResult:
    task_id: str
    status: str
    final_text: str = ""
    error: str | None = None
