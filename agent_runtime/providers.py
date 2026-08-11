from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv

from .models import ModelResponse, ToolCall


class AnthropicModel:
    """Thin adapter for the repository's existing Anthropic-compatible env setup."""

    def __init__(self, model: str | None = None, timeout: float | None = None):
        load_dotenv(override=True)
        from anthropic import Anthropic

        base_url = os.getenv("ANTHROPIC_BASE_URL")
        if base_url:
            os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
        self.name = model or os.environ["MODEL_ID"]
        configured_timeout = timeout
        if configured_timeout is None:
            configured_timeout = float(os.getenv("AGENT_RUNTIME_MODEL_TIMEOUT_SECONDS", "120"))
        if configured_timeout <= 0:
            raise ValueError("Model timeout must be positive")
        self.timeout = float(configured_timeout)
        kwargs: dict[str, Any] = {"timeout": self.timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = Anthropic(**kwargs)

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        response = self.client.messages.create(
            model=self.name,
            messages=messages,
            tools=tools,
            max_tokens=8000,
        )
        calls: list[ToolCall] = []
        text_blocks: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input)))
            elif block.type == "text":
                text_blocks.append(block.text)
        usage = {
            "input_tokens": int(getattr(response.usage, "input_tokens", 0)),
            "output_tokens": int(getattr(response.usage, "output_tokens", 0)),
        }
        raw = response.model_dump() if hasattr(response, "model_dump") else None
        return ModelResponse(
            text="\n".join(text_blocks),
            tool_calls=calls,
            stop_reason=response.stop_reason,
            usage=usage,
            raw=raw,
        )
