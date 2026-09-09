from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_runtime.providers import AnthropicModel


class _CapturingMessages:
    def __init__(self) -> None:
        self.request: dict | None = None

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(
            content=[
                SimpleNamespace(
                    type="tool_use",
                    id=f"call-{index}",
                    name=tool["name"],
                    input={"prompt": "inspect the repository"},
                )
                for index, tool in enumerate(kwargs["tools"], start=1)
            ],
            usage=SimpleNamespace(input_tokens=10, output_tokens=4),
            stop_reason="tool_use",
        )


def test_provider_maps_internal_tool_name_at_wire_boundary() -> None:
    messages_api = _CapturingMessages()
    model = AnthropicModel.__new__(AnthropicModel)
    model.name = "deepseek-chat"
    model.timeout = 120.0
    model.client = SimpleNamespace(messages=messages_api)
    schemas = [
        {
            "name": ".agent_runtime.spawn_subagent",
            "description": "Spawn a child agent.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": ".agent_runtime.request_plan_approval",
            "description": "Request plan approval.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    messages = [
        {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "previous-call",
                "name": ".agent_runtime.spawn_subagent",
                "input": {"prompt": "inspect the repository"},
            }],
        },
        {
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "previous-call",
                "content": "completed",
            }],
        },
    ]

    response = model.complete(messages, schemas)

    assert messages_api.request is not None
    assert [tool["name"] for tool in messages_api.request["tools"]] == [
        "agent_runtime_spawn_subagent",
        "agent_runtime_request_plan_approval",
    ]
    assert [tool["name"] for tool in schemas] == [
        ".agent_runtime.spawn_subagent",
        ".agent_runtime.request_plan_approval",
    ]
    assert messages_api.request["messages"][0]["content"][0]["name"] == (
        "agent_runtime_spawn_subagent"
    )
    assert messages[0]["content"][0]["name"] == ".agent_runtime.spawn_subagent"
    assert [call.name for call in response.tool_calls] == [
        ".agent_runtime.spawn_subagent",
        ".agent_runtime.request_plan_approval",
    ]


def test_provider_rejects_internal_name_collision_before_wire_call() -> None:
    messages_api = _CapturingMessages()
    model = AnthropicModel.__new__(AnthropicModel)
    model.name = "deepseek-chat"
    model.timeout = 120.0
    model.client = SimpleNamespace(messages=messages_api)
    schemas = [
        {
            "name": ".agent_runtime.spawn_subagent",
            "description": "Spawn a child agent.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "agent_runtime_spawn_subagent",
            "description": "A conflicting provider-visible tool.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    with pytest.raises(ValueError, match="Provider tool name collision"):
        model.complete([], schemas)

    assert messages_api.request is None
