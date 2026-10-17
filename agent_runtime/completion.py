"""Opt-in task completion contracts; verifier authority remains in the existing ledger."""
from dataclasses import asdict, dataclass
import hashlib
import json


@dataclass(frozen=True)
class CompletionContract:
    task_type: str
    max_stop_checks: int = 2
    version: str = "1"

    def __post_init__(self):
        if self.task_type not in {"verified_task", "candidate_patch", "swebench_repair"}:
            raise ValueError("Unsupported completion task type")
        if type(self.max_stop_checks) is not int or not 1 <= self.max_stop_checks <= 3:
            raise ValueError("max_stop_checks must be between 1 and 3")
        if self.version != "1":
            raise ValueError("Unsupported completion contract version")

    def manifest(self):
        return asdict(self)


def profile_manifest(contract, dag, advisor):
    if contract is None and advisor is None:
        return None  # Keep legacy checkpoints byte-compatible.
    value = {"completion_contract": contract.manifest() if contract else None,
             "dag_hash": dag.dag_hash if contract else None,
             "workspace_verifier": dict(getattr(dag.nodes[0].verifier, "identity", {})) if contract else None,
             "recovery_advisor": advisor.manifest() if advisor else None}
    value["sha256"] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def declared_stop_reason(text):
    """Explicit contract signals only; never infer intent from natural-language prose."""
    last = text.strip().splitlines()[-1] if text.strip() else ""
    return {"TASK_GAVE_UP": "AGENT_GAVE_UP", "TASK_NEEDS_INPUT": "NEEDS_INPUT"}.get(last)


def terminal_error(code, contract):
    return json.dumps({"code": code, "task_type": contract.task_type,
                       "contract_version": contract.version}, sort_keys=True)
