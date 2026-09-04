"""Internal stale-evidence recovery for verified-subtask plans.

The public Runtime surface remains ``run``/``resume``.  This module owns the
filesystem observation and frozen-verifier recovery policy so callers do not
need to know how checkpoint lifecycle state is projected.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .models import (
    VerifierContext,
    VerifierResult,
    VerifiedSubtaskConfig,
    is_valid_sha256,
    normalize_evidence_path,
)
from .store import EventStore, InvariantViolation


@dataclass(frozen=True)
class EvidenceSnapshot:
    manifest: list[dict[str, str]]
    complete: bool
    reason: str | None = None


@dataclass(frozen=True)
class EvidenceRecoveryReport:
    refreshed: int = 0
    invalidated: int = 0


def capture_evidence_manifest(
    repo_root: str | Path,
    manifest: list[dict[str, str]] | tuple[dict[str, str], ...],
) -> EvidenceSnapshot:
    """Capture a deterministic, fail-closed SHA-256 manifest for repository files."""

    root = Path(repo_root).resolve()
    paths: list[str] = []
    try:
        for entry in manifest:
            if not isinstance(entry, dict) or "path" not in entry:
                return EvidenceSnapshot([], False, "evidence manifest entry is malformed")
            paths.append(normalize_evidence_path(str(entry["path"])))
    except (TypeError, ValueError) as exc:
        return EvidenceSnapshot([], False, str(exc)[:256])
    if len(paths) != len(set(paths)):
        return EvidenceSnapshot([], False, "evidence manifest contains duplicate paths")

    observed: list[dict[str, str]] = []
    for relative_path in sorted(paths):
        candidate = root.joinpath(*relative_path.split("/"))
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            return EvidenceSnapshot([], False, f"evidence path escapes repository: {relative_path}: {exc}")
        try:
            if not resolved.exists():
                return EvidenceSnapshot([], False, f"evidence file is missing: {relative_path}")
            if not stat.S_ISREG(resolved.stat().st_mode):
                return EvidenceSnapshot([], False, f"evidence path is not a regular file: {relative_path}")
            # A symlink is acceptable only when its resolved target remains in
            # the repository.  The resolved containment check above is the
            # security decision; reading the resolved target avoids following
            # a target that changes through the original path during hashing.
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except (OSError, PermissionError) as exc:
            return EvidenceSnapshot([], False, f"evidence file is unreadable: {relative_path}: {exc}")
        observed.append({"path": relative_path, "sha256": digest})
    return EvidenceSnapshot(observed, True)


class VerifiedEvidenceRecovery:
    """Deep internal module that validates and repairs stale verified evidence."""

    def __init__(
        self,
        store: EventStore,
        repo_root: str | Path,
        configs: Mapping[str, VerifiedSubtaskConfig],
    ) -> None:
        self.store = store
        self.repo_root = Path(repo_root).resolve()
        self.configs = configs

    def recover(self, task_id: str) -> EvidenceRecoveryReport:
        plan = self.store.get_latest_plan(task_id)
        if plan is None:
            return EvidenceRecoveryReport()
        refreshed = 0
        invalidated = 0
        # Frozen configuration order is the scan order.  Stop at the first
        # destructive result so the next Runtime loop can resume that node.
        for item in plan.get("items", []):
            if item.get("status") != "completed":
                continue
            checkpoint = self.store.get_current_verified_subtask_checkpoint(
                task_id, int(item["plan_item_id"])
            )
            if checkpoint is None:
                raise InvariantViolation(
                    f"completed plan item {item['plan_item_id']} has no current valid checkpoint"
                )
            snapshot = capture_evidence_manifest(self.repo_root, checkpoint["evidence_manifest"])
            if not self._is_stale(checkpoint, snapshot):
                continue
            config = self.configs.get(str(item["subtask_id"]))
            if config is None:
                raise InvariantViolation(
                    f"plan item has no frozen verifier configuration: {item['subtask_id']!r}"
                )
            status, summary, manifest, observed = self._revalidate(config, checkpoint)
            if status == "pass":
                self.store.refresh_verified_subtask(
                    task_id=task_id,
                    plan_item_id=int(item["plan_item_id"]),
                    subtask_id=config.subtask_id,
                    old_checkpoint_id=int(checkpoint["verified_subtask_checkpoint_id"]),
                    completion_summary=str(checkpoint["completion_summary"]),
                    verifier_summary=summary,
                    evidence_manifest=manifest,
                    verifier_id=config.verifier_id,
                    verifier_version=config.verifier_version,
                    verification_rule=config.verification_rule,
                    verifier_bundle_hash=config.verifier_bundle_hash,
                    verifier_implementation_hash=config.verifier_implementation_hash,
                    execution_checkpoint_id=int(checkpoint["execution_checkpoint_id"]),
                    observed_manifest=observed.manifest,
                    observation_complete=observed.complete,
                )
                refreshed += 1
                continue
            self.store.invalidate_verified_subtask_evidence(
                task_id=task_id,
                plan_item_id=int(item["plan_item_id"]),
                subtask_id=config.subtask_id,
                checkpoint_id=int(checkpoint["verified_subtask_checkpoint_id"]),
                status=status,
                summary=summary,
                completion_summary=str(checkpoint["completion_summary"]),
                evidence_manifest=manifest,
                verifier_id=config.verifier_id,
                verifier_version=config.verifier_version,
                verification_rule=config.verification_rule,
                verifier_bundle_hash=config.verifier_bundle_hash,
                verifier_implementation_hash=config.verifier_implementation_hash,
                execution_checkpoint_id=int(checkpoint["execution_checkpoint_id"]),
                reason=snapshot.reason or "evidence manifest mismatch",
            )
            invalidated += 1
            break
        return EvidenceRecoveryReport(refreshed=refreshed, invalidated=invalidated)

    @staticmethod
    def _is_stale(checkpoint: dict[str, Any], snapshot: EvidenceSnapshot) -> bool:
        if int(checkpoint.get("observation_complete") or 0):
            return not snapshot.complete or snapshot.manifest != checkpoint.get("observed_manifest", [])
        # An incomplete observation is itself an evidence mismatch.  A
        # missing, unreadable, non-regular, or escaped path must never reach a
        # completed fast path without revalidation.
        return not snapshot.complete or snapshot.manifest != checkpoint.get("evidence_manifest", [])

    def _revalidate(
        self,
        config: VerifiedSubtaskConfig,
        checkpoint: dict[str, Any],
    ) -> tuple[str, str, list[dict[str, str]], EvidenceSnapshot]:
        context = VerifierContext(
            repo_root=str(self.repo_root),
            task_id=str(checkpoint["task_id"]),
            plan_item_id=int(checkpoint["plan_item_id"]),
            subtask_id=config.subtask_id,
            completion_summary=str(checkpoint["completion_summary"]),
            execution_checkpoint_id=int(checkpoint["execution_checkpoint_id"]),
        )
        try:
            result = config.verifier(context)
        except Exception as exc:
            return "uncertain", f"Verifier raised {type(exc).__name__}: {str(exc)[:256]}", [], EvidenceSnapshot([], False, str(exc)[:256])
        if not isinstance(result, VerifierResult):
            return "uncertain", "Verifier returned a malformed result; expected VerifierResult.", [], EvidenceSnapshot([], False, "malformed verifier result")
        try:
            manifest = self._normalize_manifest(result.evidence_manifest)
        except (TypeError, ValueError) as exc:
            return "uncertain", f"Verifier returned an invalid evidence manifest: {str(exc)[:256]}", [], EvidenceSnapshot([], False, str(exc)[:256])
        invalid_hashes = [entry["path"] for entry in manifest if not is_valid_sha256(entry["sha256"])]
        if result.status == "pass" and invalid_hashes:
            return "uncertain", "Verifier pass rejected because evidence contains invalid SHA-256 values: " + ", ".join(invalid_hashes), manifest, EvidenceSnapshot([], False, "invalid SHA-256")
        covered_paths = {entry["path"] for entry in manifest}
        missing_paths = sorted(set(config.evidence_paths) - covered_paths)
        if result.status == "pass" and missing_paths:
            return "uncertain", "Verifier pass rejected because evidence is incomplete: " + ", ".join(missing_paths), manifest, EvidenceSnapshot([], False, "incomplete evidence")
        if result.status != "pass":
            return result.status, result.summary, manifest, capture_evidence_manifest(self.repo_root, manifest)
        observed = capture_evidence_manifest(self.repo_root, manifest)
        if not observed.complete or observed.manifest != manifest:
            reason = observed.reason or "evidence changed while verifier was running"
            return "uncertain", "Verifier pass rejected because evidence changed during revalidation: " + reason, manifest, observed
        return "pass", result.summary, manifest, observed

    @staticmethod
    def _normalize_manifest(manifest: list[dict[str, str]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict) or "path" not in entry or "sha256" not in entry:
                raise ValueError("evidence manifest entries require path and sha256")
            path = normalize_evidence_path(str(entry["path"]))
            if path in seen:
                raise ValueError(f"duplicate evidence path: {path}")
            seen.add(path)
            normalized.append({"path": path, "sha256": str(entry["sha256"])})
        return sorted(normalized, key=lambda entry: entry["path"])


__all__ = ["EvidenceRecoveryReport", "EvidenceSnapshot", "VerifiedEvidenceRecovery", "capture_evidence_manifest"]
