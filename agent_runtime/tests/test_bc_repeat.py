from agent_runtime.experiments.bc_repeat import classify


def call(number, text, tools=()):
    return {"call": number, "response": {"content": [{"type": "text", "text": text}, *tools]}}


def test_marker_categories_match_runtime_exact_line_contract():
    assert classify([call(1, "Done\nSUBTASK_COMPLETE")], "parser.py") == [{"call": 1, "kind": "marker_valid"}]
    assert classify([call(1, "SUBTASK_COMPLETE: done")], "parser.py") == [{"call": 1, "kind": "marker_format_error"}]
    assert classify([call(1, "Done")], "parser.py") == [{"call": 1, "kind": "text_without_marker"}]
    tool = {"type": "tool_use", "name": "write_file", "input": {"path": "target.py"}}
    assert classify([call(1, "SUBTASK_COMPLETE", [tool])], "parser.py") == [{"call": 1, "kind": "marker_with_tools"}]


def test_reads_and_api_errors_are_separate_from_protocol_errors():
    read = {"type": "tool_use", "name": "read_file", "input": {"path": "./parser.py"}}
    events = classify([call(1, "Reading", [read]), call(2, "", [read]), {"call": 3, "error_type": "Timeout"}], "parser.py")
    assert [e["kind"] for e in events] == ["completed_file_read", "completed_file_read", "repeated_read", "api_error"]
