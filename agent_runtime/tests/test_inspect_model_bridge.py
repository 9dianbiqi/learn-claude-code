"""Protocol integrity checks using the actual optional Inspect classes."""
import pytest

pytest.importorskip("inspect_ai", reason="optional Inspect dependency")

from inspect_ai.model import ChatMessageAssistant, ChatCompletionChoice, ModelOutput, ModelUsage
from inspect_ai.tool import ToolCall as InspectToolCall

from agent_runtime.inspect_model_bridge import messages_to_inspect, response_from_inspect, mock_output, tools_to_inspect
from agent_runtime.models import ModelResponse, ToolCall


def test_text_tools_multiple_results_and_error_flags_preserved():
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "question"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "reason"},
            {"type": "tool_use", "id": "one", "name": "read_file", "input": {"path": "a"}},
            {"type": "tool_use", "id": "two", "name": "read_file", "input": {"path": "b"}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "one", "content": "A"},
            {"type": "tool_result", "tool_use_id": "two", "content": "PATH_NOT_FOUND", "is_error": True},
            {"type": "text", "text": "continue"}]}]
    converted = messages_to_inspect(messages)
    assert [m.role for m in converted] == ["system", "user", "assistant", "tool", "tool", "user"]
    assert converted[2].text == "reason"
    assert [(c.id, c.function, c.arguments) for c in converted[2].tool_calls] == [
        ("one", "read_file", {"path": "a"}), ("two", "read_file", {"path": "b"})]
    assert converted[3].tool_call_id == "one" and converted[3].error is None
    assert converted[4].tool_call_id == "two" and converted[4].error.message == "PATH_NOT_FOUND"
    assert converted[5].text == "continue"


@pytest.mark.parametrize("message", [
    {"role": "user", "content": [{"type": "image", "data": "not supported"}]},
    {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "x", "input": {}}, {"type": "text", "text": "interleaved"}]},
    {"role": "system", "content": []}, {"role": "unexpected", "content": "x"}])
def test_unsupported_content_rejected_instead_of_dropped(message):
    with pytest.raises(ValueError):
        messages_to_inspect([message])


def test_inspect_response_round_trip_ids_arguments_and_usage():
    original = ModelResponse(text="text", tool_calls=[ToolCall("a", "read_file", {"path": "a.txt"})],
                             usage={"input_tokens": 3, "output_tokens": 4})
    output = mock_output(original)
    output.usage.input_tokens_cache_read = 2
    converted = response_from_inspect(output)
    assert converted.text == "text" and converted.tool_calls == original.tool_calls
    assert converted.stop_reason == "tool_use"
    assert converted.usage["input_tokens"] == 3 and converted.usage["output_tokens"] == 4
    assert converted.usage["input_tokens_cache_read"] == 2
    assert converted.raw["choices"][0]["message"]["tool_calls"][0]["id"] == "a"


def test_missing_usage_not_invented_and_truncation_reason_preserved():
    output = ModelOutput(model="mockllm/runtime-bridge", choices=[ChatCompletionChoice(
        message=ChatMessageAssistant(content="truncated"), stop_reason="max_tokens")])
    converted = response_from_inspect(output)
    assert converted.usage == {} and converted.stop_reason == "max_tokens"


def test_malformed_tool_output_and_extra_choices_rejected():
    output = ModelOutput(model="mockllm/runtime-bridge", choices=[ChatCompletionChoice(
        message=ChatMessageAssistant(content="", tool_calls=[InspectToolCall("x", "read_file", {}, parse_error="bad JSON")]),
        stop_reason="tool_calls")])
    with pytest.raises(ValueError, match="malformed"):
        response_from_inspect(output)
    output.choices = []
    with pytest.raises(ValueError, match="one Inspect"):
        response_from_inspect(output)


def test_tool_schema_constraints_preserved():
    schema = {"name": "read_file", "description": "read", "input_schema": {
        "type": "object", "properties": {"path": {"type": "string", "maxLength": 400}},
        "required": ["path"], "additionalProperties": False}}
    result = tools_to_inspect([schema])[0]
    assert result.name == "read_file" and result.parameters.required == ["path"]
    encoded = result.parameters.model_dump(exclude_none=True)
    assert encoded["additionalProperties"] is False and encoded["properties"]["path"]["maxLength"] == 400
