"""Regression: F3 after a text-only completion must not replay an empty tool batch."""
import hashlib
import json

import pytest

from agent_runtime.fake_model import ScriptedModel
from agent_runtime.projector import ContextProjection, estimate_tokens
from agent_runtime.models import ModelResponse, VerifiedSubtaskConfig, VerifiedSubtaskDAGConfig, VerifierResult
from agent_runtime.runtime import Runtime, InjectedCrash


class FullHistory:
    """Exercise provider-visible full history without experiment dependencies."""

    def project(self, task_id, messages, checkpoint_id):
        return ContextProjection(messages, checkpoint_id, {
            "basis": "full_history", "projection_used": False,
            "token_estimate": estimate_tokens(json.dumps(messages, ensure_ascii=False)),
        })


@pytest.mark.parametrize("scoped", [False, True])
def test_f3_resume_never_sends_empty_user_tool_results(tmp_path, scoped):
    (tmp_path / "artifact.txt").write_text("valid", encoding="utf-8")

    def verify(context):
        return VerifierResult("pass", "valid", [{"path": "artifact.txt", "sha256": hashlib.sha256(b"valid").hexdigest()}])

    def node(name, deps=()):
        return VerifiedSubtaskConfig(name, name, name, ("artifact.txt",), name, "1", "valid",
                                     verify, "b"*64, blocked_by=deps, max_turns=3)

    dag = VerifiedSubtaskDAGConfig(nodes=(node("first"), node("second", ("first",))))

    def crash(point, **context):
        if point == "verified_subtask_f3":
            raise InjectedCrash(point)

    first = Runtime(tmp_path, ScriptedModel([ModelResponse(text="First complete.\nSUBTASK_COMPLETE")]),
                    verified_subtask_dag=dag, fault_injector=crash)
    first.projector = FullHistory()
    with pytest.raises(InjectedCrash):
        first.run("Complete both subtasks")
    task_id = first.store.list_tasks()[0]["task_id"]
    model = ScriptedModel([ModelResponse(text="Second complete.\nSUBTASK_COMPLETE")])
    resumed = Runtime(tmp_path, model, verified_subtask_dag=dag,
                      scoped_context_budget=16000 if scoped else None)
    resumed.projector = FullHistory()
    result = resumed.resume(task_id)
    assert result.status == "completed"
    assert model.calls
    assert all(message.get("content") for message in model.calls[0]), model.calls[0]
    assert model.calls[0][-1]["role"] == "user"
    assert "Continue subtask second" in model.calls[0][-1]["content"]
    assert resumed.store.get_plan_item(task_id, "second")["consumed_turns"] == 1
