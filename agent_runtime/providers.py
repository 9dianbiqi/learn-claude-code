from __future__ import annotations

import copy
import os
from typing import Any

from dotenv import load_dotenv

from .models import ModelResponse, ToolCall


_INTERNAL_TO_PROVIDER_TOOL_NAME = {
    ".agent_runtime.spawn_subagent": "agent_runtime_spawn_subagent",
    ".agent_runtime.request_plan_approval": "agent_runtime_request_plan_approval",
}


def _provider_tool_schemas(
    tools: list[dict],
) -> tuple[list[dict], dict[str, str]]:
    provider_tools: list[dict] = []
    provider_to_internal: dict[str, str] = {}
    for tool in tools:
        internal_name = str(tool["name"])
        provider_name = _INTERNAL_TO_PROVIDER_TOOL_NAME.get(internal_name, internal_name)
        existing = provider_to_internal.get(provider_name)
        if existing is not None and existing != internal_name:
            raise ValueError(
                "Provider tool name collision: "
                f"{existing!r} and {internal_name!r} both map to {provider_name!r}"
            )
        provider_to_internal[provider_name] = internal_name
        provider_tool = dict(tool)
        provider_tool["name"] = provider_name
        provider_tools.append(provider_tool)
    return provider_tools, provider_to_internal


def _provider_messages(messages: list[dict]) -> list[dict]:
    provider_messages = copy.deepcopy(messages)
    for message in provider_messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            internal_name = block.get("name")
            if isinstance(internal_name, str):
                block["name"] = _INTERNAL_TO_PROVIDER_TOOL_NAME.get(
                    internal_name,
                    internal_name,
                )
    return provider_messages


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
        provider_tools, provider_to_internal = _provider_tool_schemas(tools)
        response = self.client.messages.create(
            model=self.name,
            messages=_provider_messages(messages),
            tools=provider_tools,
            max_tokens=8000,
        )
        calls: list[ToolCall] = []
        text_blocks: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                internal_name = provider_to_internal.get(block.name, block.name)
                calls.append(ToolCall(block.id, internal_name, dict(block.input)))
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
