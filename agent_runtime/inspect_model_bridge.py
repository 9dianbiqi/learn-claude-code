"""Text/function-tool protocol bridge; Runtime owns execution and recovery."""
from __future__ import annotations

import json
from pathlib import Path

import anyio
from inspect_ai.model import (
    ChatMessageAssistant, ChatMessageSystem, ChatMessageTool, ChatMessageUser,
    ChatCompletionChoice, ModelOutput, ModelUsage,
)
from inspect_ai.tool import ToolCall as InspectToolCall, ToolCallError, ToolInfo

from .evaluation import _dump, _hash
from .models import ModelResponse, ToolCall


def messages_to_inspect(messages: list[dict]) -> list:
    converted = []
    names = {}
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            constructor = {"user": ChatMessageUser, "assistant": ChatMessageAssistant,
                           "system": ChatMessageSystem}.get(role)
            if constructor is None:
                raise ValueError("unsupported Runtime conversation role")
            converted.append(constructor(content=content))
            continue
        if not isinstance(content, list) or role not in {"user", "assistant"}:
            raise ValueError("unsupported Runtime conversation content")
        if role == "assistant":
            text, calls = [], []
            for block in content:
                if block.get("type") == "text" and not calls and isinstance(block.get("text"), str):
                    text.append({"type": "text", "text": block["text"]})
                elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    calls.append(InspectToolCall(block["id"], block["name"], block["input"]))
                    names[block["id"]] = block["name"]
                else:
                    raise ValueError("unsupported or interleaved assistant block")
            converted.append(ChatMessageAssistant(content=text or "", tool_calls=calls or None))
        else:
            for block in content:
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    converted.append(ChatMessageUser(content=block["text"]))
                elif block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                    error = ToolCallError("unknown", block["content"]) if block.get("is_error") else None
                    converted.append(ChatMessageTool(content=block["content"],
                        tool_call_id=block["tool_use_id"], function=names.get(block["tool_use_id"]), error=error))
                else:
                    raise ValueError("unsupported user/tool result block")
    return converted


def tools_to_inspect(tools: list[dict]) -> list[ToolInfo]:
    return [ToolInfo(name=t["name"], description=t["description"], parameters=t["input_schema"]) for t in tools]


def response_from_inspect(output: ModelOutput) -> ModelResponse:
    if len(output.choices) != 1:
        raise ValueError("exactly one Inspect completion choice required")
    choice = output.choices[0]
    message = choice.message
    content = message.content
    if isinstance(content, str):
        text = content
    else:
        if any(block.type != "text" for block in content):
            raise ValueError("unsupported non-text Inspect content")
        text = "".join(block.text for block in content)
    calls = []
    for call in message.tool_calls or []:
        if call.parse_error or call.type != "function":
            raise ValueError("malformed or unsupported Inspect tool call")
        calls.append(ToolCall(call.id, call.function, dict(call.arguments)))
    usage = {} if output.usage is None else {
        k: v for k, v in output.usage.model_dump(exclude_none=True).items() if type(v) is int}
    reason = {"stop": "end_turn", "tool_calls": "tool_use"}.get(choice.stop_reason, choice.stop_reason)
    return ModelResponse(text=text, tool_calls=calls, stop_reason=reason,
                         usage=usage, raw=output.model_dump(mode="json"))


def mock_output(response: ModelResponse) -> ModelOutput:
    """Create a real Inspect output with explicitly synthetic usage."""
    message = ChatMessageAssistant(content=response.text, tool_calls=[
        InspectToolCall(c.id, c.name, c.input) for c in response.tool_calls] or None)
    usage = response.usage or {"input_tokens": 1, "output_tokens": 1}
    return ModelOutput(model="mockllm/runtime-bridge", choices=[ChatCompletionChoice(
        message=message, stop_reason="tool_calls" if response.tool_calls else "stop")],
        usage=ModelUsage(input_tokens=usage.get("input_tokens", 1),
                         output_tokens=usage.get("output_tokens", 1),
                         total_tokens=usage.get("input_tokens", 1) + usage.get("output_tokens", 1)))


class InspectModelBridge:
    """Synchronous Runtime model backed by an explicit local Inspect model.

    Must be called on an AnyIO worker thread. Receipts are audit evidence only;
    they never supply recovery responses to Runtime.
    """

    name = "inspect-mock-runtime-bridge"

    def __init__(self, model, output: Path, case_id: str):
        if str(model) != "mockllm/runtime-bridge" or model.api.base_url is not None:
            raise ValueError("controlled bridge accepts only its explicit local mock model")
        self.model, self.case_id = model, case_id
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=False)
        self.store = None
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelResponse:
        if self.store is None:
            raise RuntimeError("bridge must be bound to the Runtime ledger")
        tasks = self.store.list_tasks()
        if len(tasks) != 1:
            raise ValueError("one Runtime task per Inspect case required")
        task_id = tasks[0]["task_id"]
        records = self.store.list_model_calls(task_id)
        pending = records[-1]
        if pending["status"] != "started":
            raise ValueError("a pending Runtime model call is required")
        request = {"messages": messages, "tools": tools}
        encoded = json.dumps(request, sort_keys=True, ensure_ascii=False).encode()
        if len(encoded) > 128 * 1024:
            raise ValueError("bridge request exceeds 128 KiB")
        converted, schemas = messages_to_inspect(messages), tools_to_inspect(tools)
        self.calls += 1
        receipt = {"case_id": self.case_id, "task_id": task_id,
                   "runtime_model_call_id": pending["model_call_id"], "turn": pending["turn"],
                   "bridge_call": self.calls, "request_sha256": _hash(encoded), "runtime_request": request,
                   "ledger_request_sha256": _hash(json.dumps(pending["request"], sort_keys=True, ensure_ascii=False).encode()),
                   "projection": pending.get("projection"), "source_checkpoint_id": pending.get("source_checkpoint_id"),
                   "usage_kind": "synthetic_mock", "ok": False}

        async def generate():
            # A cancelled Inspect host scope must not strand from_thread.run's
            # synchronous waiter. Finish this bounded mock generation before join.
            with anyio.CancelScope(shield=True):
                with anyio.fail_after(15):
                    return await self.model.generate(input=converted, tools=schemas, tool_choice="auto", cache=False)

        try:
            output = anyio.from_thread.run(generate)
            receipt["inspect_output"] = output.model_dump(mode="json")
            response = response_from_inspect(output)
            receipt.update(ok=True, usage=response.usage)
            return response
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            _dump(self.output / f"{self.calls:03d}.json", receipt)
