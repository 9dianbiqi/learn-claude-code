"""Task-type adapters to the existing VerifierResult interface.

The trusted workspace backend supplies freeze/capture/validate. It must keep the
covered workspace quiescent throughout verification AND the SQLite commit.
No model output or model-writable test report is treated as validation authority.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from contextvars import ContextVar
from types import MappingProxyType

from .models import VerifierResult, is_valid_sha256


class WorkspaceCleanupError(RuntimeError):
    """Verification authority and external resource cleanup have separate outcomes.

    Callers must inspect the durable task status and this cleanup report. This
    exception never authorizes retrying a mutation or declaring cleanup complete.
    """
    def __init__(self, report):
        self.report = report
        super().__init__("Workspace cleanup requires review: "+json.dumps(report, sort_keys=True))


@dataclass(frozen=True)
class WorkspaceSnapshot:
    workspace_id: str
    instance_id: str
    base_commit: str
    revision: str
    patch: str
    patch_applies: bool

    @property
    def patch_sha256(self):
        return hashlib.sha256(self.patch.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ValidationEvidence:
    revision: str
    patch_sha256: str
    validator_id: str
    exit_code: int | None
    tests_run: int
    official_resolved: bool | None = None
    dataset_revision: str | None = None
    harness_commit: str | None = None


class WorkspaceVerifier:
    evidence_path = ".agent_runtime/verification/completion.json"

    def __init__(self, *, goal, workspace_id, instance_id, base_commit, validator_id,
                 freeze, capture, validate, dataset_revision=None, harness_commit=None):
        if goal not in {"candidate_patch", "swebench_repair"}:
            raise ValueError("Unknown workspace goal")
        if not all(isinstance(v, str) and v for v in
                   (workspace_id, instance_id, base_commit, validator_id)):
            raise ValueError("Frozen workspace and validator identities required")
        if not all(callable(v) for v in (freeze, capture, validate)):
            raise TypeError("Trusted workspace functions required")
        if goal == "swebench_repair" and (not dataset_revision or not harness_commit):
            raise ValueError("Official dataset/harness identity required")
        self.goal, self.freeze, self.capture, self.validate = goal, freeze, capture, validate
        self.identity = MappingProxyType({"workspace_id": workspace_id, "instance_id": instance_id,
                         "base_commit": base_commit, "validator_id": validator_id,
                         "dataset_revision": dataset_revision, "harness_commit": harness_commit,
                         "goal": goal})
        self._held = ContextVar("workspace_verification_scope", default=False)

    def verification_scope(self):
        from contextlib import contextmanager
        @contextmanager
        def hold():
            if self._held.get():
                yield
                return
            with self.freeze():
                token = self._held.set(True)
                try:
                    yield
                finally:
                    self._held.reset(token)
        return hold()

    def evidence_is_current(self, repo_root, checkpoint):
        if not self._held.get():
            return False
        try:
            saved = json.loads((Path(repo_root)/self.evidence_path).read_text(encoding="utf-8"))
            return saved["workspace"] == asdict(self.capture())
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def __call__(self, context):
        if not self._held.get():
            return VerifierResult("uncertain", "Workspace must remain frozen through commit", [])
        before = self.capture()
        if not isinstance(before, WorkspaceSnapshot) or not is_valid_sha256(before.revision):
            return VerifierResult("uncertain", "Malformed workspace snapshot", [])
        if any(getattr(before, k) != self.identity[k]
               for k in ("workspace_id", "instance_id", "base_commit")):
            return VerifierResult("uncertain", "Workspace identity changed", [])
        if not before.patch.strip() or before.patch_applies is not True:
            return VerifierResult("fail", "Missing or non-applicable candidate patch", [])
        checked = self.validate(before)
        after = self.capture()
        if after != before:
            return VerifierResult("uncertain", "Workspace changed during validation", [])
        if not isinstance(checked, ValidationEvidence):
            return VerifierResult("uncertain", "Validation is unavailable or malformed", [])
        if (checked.revision != before.revision or checked.patch_sha256 != before.patch_sha256
                or checked.validator_id != self.identity["validator_id"]):
            return VerifierResult("uncertain", "Validation does not cover this candidate", [])
        if checked.exit_code is None or type(checked.tests_run) is not int or checked.tests_run < 1:
            return VerifierResult("uncertain", "Verification evidence is missing", [])
        if type(checked.exit_code) is not int or checked.exit_code != 0:
            return VerifierResult("fail", "Validation command failed", [])
        if self.goal == "swebench_repair":
            if (checked.dataset_revision != self.identity["dataset_revision"]
                    or checked.harness_commit != self.identity["harness_commit"]):
                return VerifierResult("uncertain", "Official evaluation identity mismatch", [])
            if checked.official_resolved is None:
                return VerifierResult("uncertain", "Official evaluation not authorized/available", [])
            if checked.official_resolved is not True:
                return VerifierResult("fail", "Official instance unresolved", [])
        payload = {"workspace": asdict(before), "validation": asdict(checked), "goal": self.goal}
        data = (json.dumps(payload, sort_keys=True)+"\n").encode("utf-8")
        target = Path(context.repo_root)/self.evidence_path
        # Do not follow a model-writable symlink out of the controller repository.
        target.resolve().relative_to(Path(context.repo_root).resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return VerifierResult("pass", "Verified "+self.goal,
                              [{"path": self.evidence_path, "sha256": hashlib.sha256(data).hexdigest()}])
