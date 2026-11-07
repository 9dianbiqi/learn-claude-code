"""Versioned result-only evaluation contract. No execution or Runtime DB writes.

An observation is not necessarily an identified execution. Identity completeness,
evidence integrity, oracle authority and usage completeness are separate facts.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re
from typing import Any

SCHEMA = "evaluation-run-v2"
VERDICTS = {"pass", "fail", "blocked", "unknown"}
ATTEMPT_KINDS = {"suite_invocation", "case_execution", "model_generation",
                 "verifier_invocation", "tool_operation"}
COMPARISON_FIELDS = {"task_version", "condition", "model", "budget", "fault", "seed", "repeat"}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("lowercase SHA-256 required")
    return value


@dataclass(frozen=True)
class ExecutionScope:
    model_mode: str = "unknown"
    environment: str = "unknown"
    grading: str = "unknown"
    subject_role: str = "unknown"

    def __post_init__(self):
        enums = {"model_mode": {"scripted", "mock", "live", "none", "unknown"},
                 "environment": {"local", "simulated_container", "real_container", "unknown"},
                 "grading": {"runtime_oracle", "controller_verifier", "official_harness",
                             "test_assertion", "unknown"},
                 "subject_role": {"runtime_regression", "agent_candidate", "scripted_candidate",
                                  "gold_control", "test_execution", "unknown"}}
        for name, values in enums.items():
            if getattr(self, name) not in values:
                raise ValueError("invalid execution scope: " + name)


@dataclass(frozen=True)
class Oracle:
    oracle_id: str
    kind: str
    authority: str = "reported"
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.oracle_id or not self.kind or self.authority not in {"reported", "source_validated", "unavailable"}:
            raise ValueError("explicit oracle identity/kind/authority required")


@dataclass(frozen=True)
class SourceExecutionIdentity:
    execution_id: str
    case_id: str
    objective: str
    oracle_id: str

    def __post_init__(self):
        if not all(isinstance(v, str) and v for v in asdict(self).values()):
            raise ValueError("complete source execution identity required")

    @property
    def key(self) -> str:
        return digest(asdict(self))


@dataclass(frozen=True)
class ComparisonIdentity:
    frozen: dict[str, Any] = field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        missing = COMPARISON_FIELDS - self.frozen.keys()
        missing |= {key for key in ("task_version", "condition", "model", "budget", "repeat")
                    if self.frozen.get(key) is None}
        return sorted(missing)

    @property
    def key(self) -> str | None:
        return None if self.missing else digest(self.frozen)


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    path: str
    sha256: str
    source_schema: str
    source_run_id: str | None = None
    integrity: str = "sha256_verified"
    provenance: str = "controller_declared"
    evidence_mode: str = "imported"
    seal_sha256: str | None = None

    def __post_init__(self):
        sha256(self.sha256)
        if self.seal_sha256 is not None:
            sha256(self.seal_sha256)
        if self.integrity != "sha256_verified" or self.provenance not in {"controller_declared", "evaluator_receipt_bound"}:
            raise ValueError("unsupported evidence integrity/provenance")
        if self.evidence_mode not in {"imported", "projection", "reused"}:
            raise ValueError("result-only contract cannot claim a new execution")


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    kind: str
    source_id: str
    evidence_refs: tuple[str, ...]
    parent_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in ATTEMPT_KINDS or not self.attempt_id or not self.source_id:
            raise ValueError("explicit attempt kind/source identity required")


@dataclass(frozen=True)
class Metric:
    name: str
    value: int | float | None
    unit: str
    complete: bool
    missing_reason: str | None = None
    evidence_refs: tuple[str, ...] = ()
    definition_version: str = "1"

    def __post_init__(self):
        if (not self.name or not self.unit or type(self.complete) is not bool
                or (self.value is not None and (type(self.value) not in {int, float}
                    or not math.isfinite(self.value) or self.value < 0))):
            raise ValueError("invalid metric")
        if self.value is None and (self.complete or not self.missing_reason):
            raise ValueError("missing metric requires incomplete status and reason")


@dataclass(frozen=True)
class EvaluationCase:
    observation_id: str
    case_id: str
    objective: str
    oracle: Oracle
    verdict: str
    reason: str
    scope: ExecutionScope
    source_identity: SourceExecutionIdentity | None = None
    comparison: ComparisonIdentity = field(default_factory=ComparisonIdentity)
    attempt_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metrics: tuple[Metric, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.verdict not in VERDICTS or not all((self.observation_id, self.case_id, self.objective, self.reason)):
            raise ValueError("case requires objective, verdict and reason")
        if self.source_identity and (self.source_identity.objective != self.objective
                                    or self.source_identity.oracle_id != self.oracle.oracle_id):
            raise ValueError("identity must bind this objective and oracle")
        if self.oracle.authority == "unavailable" and self.verdict in {"pass", "fail"}:
            raise ValueError("unavailable oracle cannot establish a known verdict")


@dataclass
class EvaluationRun:
    run_id: str
    source_schema: str
    evidence: list[Evidence] = field(default_factory=list)
    cases: list[EvaluationCase] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    metrics: list[Metric] = field(default_factory=list)
    # Counts whose planned subjects are not identified by the old result schema.
    unallocated: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    schema_version: str = field(default=SCHEMA, init=False)

    def as_dict(self) -> dict:
        value = asdict(self)
        for case, row in zip(self.cases, value["cases"]):
            row["source_identity_key"] = case.source_identity.key if case.source_identity else None
            row["comparison_identity_key"] = case.comparison.key
            row["comparison_missing"] = case.comparison.missing
        return value


def outcome_counts(counts: dict[str, int]) -> dict:
    if set(counts) != VERDICTS or any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError("four nonnegative verdict counts required")
    n = sum(counts.values())
    complete = not counts["blocked"] and not counts["unknown"]
    return {**counts, "N": n, "success_lower_bound": counts["pass"] / n if n else None,
            "strict_success_rate": counts["pass"] / n if n and complete else None,
            "known_fraction": (counts["pass"] + counts["fail"]) / n if n else None,
            "complete": bool(n and complete)}


def aggregate(runs: list[EvaluationRun]) -> dict:
    """Deduplicate observations; conflicting identified executions fail closed.

    A schema/report fingerprint identifies a repeated import only. It never
    establishes that two different unidentified reports describe one execution.
    """
    seen, selected, observations = {}, [], set()
    attempts: dict[str, str] = {}
    for run in runs:
        for attempt in run.attempts:
            fingerprint = digest({"kind": attempt.kind, "source_id": attempt.source_id,
                                  "parent_id": attempt.parent_id, "raw": attempt.raw})
            if attempt.attempt_id in attempts and attempts[attempt.attempt_id] != fingerprint:
                raise ValueError("conflicting evidence for attempt identity: " + attempt.attempt_id)
            attempts[attempt.attempt_id] = fingerprint
        for case in run.cases:
            key = case.source_identity.key if case.source_identity else "observation:" + case.observation_id
            fingerprint = digest({"verdict": case.verdict, "scope": asdict(case.scope),
                                  "objective": case.objective, "oracle": case.oracle.oracle_id,
                                  "comparison": case.comparison.frozen,
                                  "raw": case.raw,
                                  "metrics": [{k: v for k, v in asdict(m).items() if k != "evidence_refs"}
                                              for m in case.metrics]})
            if key in seen:
                if seen[key] != fingerprint:
                    raise ValueError("conflicting evidence for source execution identity: " + key)
                continue
            seen[key] = fingerprint
            observations.add(case.observation_id)
            selected.append(case)
    groups: dict[str, dict] = {}

    def group(objective, scope, comparison):
        # Repeats/seeds remain independent samples within one frozen treatment.
        # Distinct recorded models/budgets/conditions/task versions never pool.
        context = {k: v for k, v in comparison.items() if k not in {"seed", "repeat"}}
        identity = {"objective": objective, "scope": scope, "comparison_context": context}
        key = digest(identity)
        return groups.setdefault(key, {**identity, "counts": dict.fromkeys(sorted(VERDICTS), 0),
                                       "unidentified_planned": 0})

    for case in selected:
        group(case.objective, asdict(case.scope), case.comparison.frozen)["counts"][case.verdict] += 1
    gaps = {}
    for run in runs:
        for gap in run.unallocated:
            fingerprint = digest({k: v for k, v in gap.items() if k != "identity"})
            if gap["identity"] in gaps:
                if gaps[gap["identity"]] != fingerprint:
                    raise ValueError("conflicting evidence for planned denominator identity")
                continue
            gaps[gap["identity"]] = fingerprint
            cohort = group(gap["objective"], gap["scope"], gap.get("comparison", {}))
            cohort["counts"]["unknown"] += gap["count"]
            cohort["unidentified_planned"] += gap["count"]
    return {"schema_version": "evaluation-cohorts-v2", "cohorts": [
                {"cohort_id": key, "objective": row["objective"], "scope": row["scope"],
                 "comparison_context": row["comparison_context"],
                 "unidentified_planned": row["unidentified_planned"], **outcome_counts(row["counts"])}
                for key, row in sorted(groups.items())],
            "unique_case_observations": len(selected), "unique_attempt_records": len(attempts),
            "overall_success_rate": None}
