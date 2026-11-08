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
    identity_status: str = "explicit"
    identity_components: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not self.oracle_id or not self.kind or self.authority not in {"reported", "source_validated", "unavailable"}:
            raise ValueError("explicit oracle identity/kind/authority required")
        if self.identity_status not in {"explicit", "placeholder"}:
            raise ValueError("invalid oracle identity status")
        if self.identity_status == "placeholder" and self.authority == "source_validated":
            raise ValueError("placeholder oracle cannot be source validated")
        if self.identity_status == "placeholder" and self.identity_components:
            raise ValueError("placeholder oracle cannot carry explicit identity components")
        if (not isinstance(self.identity_components, dict) or
                any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                    for k, v in self.identity_components.items())):
            raise ValueError("oracle identity components must be explicit nonempty strings")


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
    seen, selected, pending, selected_index = {}, [], [], {}
    attempts: dict[str, str] = {}
    attempt_records = {}
    for run in runs:
        for attempt in run.attempts:
            fingerprint = digest({"kind": attempt.kind, "source_id": attempt.source_id,
                                  "parent_id": attempt.parent_id, "raw": attempt.raw})
            if attempt.attempt_id in attempts and attempts[attempt.attempt_id] != fingerprint:
                previous = attempt_records[attempt.attempt_id]
                if (previous.kind != attempt.kind or previous.source_id != attempt.source_id
                        or previous.raw != attempt.raw or
                        (previous.parent_id is not None and attempt.parent_id is not None
                         and previous.parent_id != attempt.parent_id)):
                    raise ValueError("conflicting evidence for attempt identity: " + attempt.attempt_id)
                if attempt.parent_id is None:
                    continue
            attempts[attempt.attempt_id] = fingerprint
            attempt_records[attempt.attempt_id] = attempt
        for case in run.cases:
            archive_run_alias = (case.source_identity is not None and case.raw.get("mode") == "agent_archive"
                                 and case.source_identity.execution_id == case.raw.get("source_run_id")
                                 and case.oracle.authority == "reported")
            if case.source_identity is None or archive_run_alias:
                pending.append(case)
                continue
            key = case.source_identity.key
            fingerprint = digest({"verdict": case.verdict, "scope": asdict(case.scope),
                                  "objective": case.objective, "oracle": case.oracle.oracle_id,
                                  "comparison": case.comparison.frozen,
                                  "raw": case.raw,
                                  "metrics": [{k: v for k, v in asdict(m).items() if k != "evidence_refs"}
                                              for m in case.metrics]})
            if key in seen:
                if seen[key] != fingerprint:
                    raise ValueError("conflicting evidence for source execution identity: " + key)
                previous = selected[selected_index[key]]
                better, weaker = sorted((previous, case), key=lambda c: _oracle_strength(c.oracle), reverse=True)
                if not _oracle_refines(weaker.oracle, better.oracle):
                    raise ValueError("conflicting oracle evidence for source execution identity")
                selected[selected_index[key]] = better
                continue
            seen[key] = fingerprint
            selected_index[key] = len(selected)
            selected.append(case)
    refined = {}
    for case in selected:
        identity = case.source_identity
        alias = (case.observation_id, identity.execution_id, identity.case_id, identity.objective)
        previous = refined.get(alias)
        if previous:
            better, weaker = sorted((previous, case), key=lambda c: _oracle_strength(c.oracle), reverse=True)
            if not _compatible_observation(weaker, better):
                raise ValueError("conflicting oracle evidence for observation/execution identity")
            refined[alias] = better
        else:
            refined[alias] = case
    selected = list(refined.values())
    identified = {}
    for case in selected:
        identified.setdefault(case.observation_id, []).append(case)
    for case in pending:
        aliases = identified.get(case.observation_id, [])
        if aliases:
            compatible = [known for known in aliases if _compatible_observation(case, known)]
            if not compatible:
                raise ValueError("conflicting evidence for observation/execution identity")
            if len(compatible) != 1:
                raise ValueError("ambiguous observation matches multiple independent executions")
            # Same exact report row, enriched by the sealed execution evidence.
            # The weaker import remains visible in runs but adds no sample.
            continue
        key = case.source_identity.key if case.source_identity else "observation:" + case.observation_id
        fingerprint = digest({"verdict": case.verdict, "scope": asdict(case.scope),
                              "comparison": case.comparison.frozen, "raw": case.raw,
                              "oracle": case.oracle.oracle_id,
                              "metrics": [{k: v for k, v in asdict(m).items() if k != "evidence_refs"}
                                          for m in case.metrics]})
        if key in seen:
            if seen[key] != fingerprint:
                raise ValueError("conflicting evidence for observation identity")
            continue
        seen[key] = fingerprint
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
    represented = {}
    for run in runs:
        if run.evidence:
            evidence_id = run.evidence[0].evidence_id
            represented[evidence_id] = max(represented.get(evidence_id, 0), len(run.cases))
    for run in runs:
        for gap in run.unallocated:
            fingerprint = digest({k: v for k, v in gap.items() if k != "identity"})
            if gap["identity"] in gaps:
                if gaps[gap["identity"]] != fingerprint:
                    raise ValueError("conflicting evidence for planned denominator identity")
                continue
            gaps[gap["identity"]] = fingerprint
            named_extra = max(0, represented.get(gap.get("evidence_id"), 0) - gap.get("observed_cases", 0))
            remaining = max(0, gap["count"] - named_extra)
            if remaining:
                cohort = group(gap["objective"], gap["scope"], gap.get("comparison", {}))
                cohort["counts"]["unknown"] += remaining
                cohort["unidentified_planned"] += remaining
    used_case_attempts = {ref for case in selected for ref in case.attempt_refs}
    unique_attempts = sum(a.kind != "case_execution" or key in used_case_attempts
                          for key, a in attempt_records.items())
    return {"schema_version": "evaluation-cohorts-v2", "cohorts": [
                {"cohort_id": key, "objective": row["objective"], "scope": row["scope"],
                 "comparison_context": row["comparison_context"],
                 "unidentified_planned": row["unidentified_planned"], **outcome_counts(row["counts"])}
                for key, row in sorted(groups.items())],
            "unique_case_observations": len(selected), "unique_attempt_records": unique_attempts,
            "overall_success_rate": None}


def _compatible_observation(observed: EvaluationCase, identified: EvaluationCase) -> bool:
    """Check an exact-byte observation alias without conflating known repeats.

    Unknown metadata may be enriched; explicit contradictory metadata may not.
    Different report bytes never enter this comparison. Distinct strong execution
    identities stay distinct even when their old reports happen to be identical.
    """
    if observed.objective != identified.objective or observed.raw != identified.raw:
        return False
    if not _oracle_refines(observed.oracle, identified.oracle):
        return False
    if observed.verdict != identified.verdict and observed.verdict != "unknown":
        return False
    for key, value in asdict(observed.scope).items():
        other = getattr(identified.scope, key)
        if value != "unknown" and other != "unknown" and value != other:
            return False
    for key in observed.comparison.frozen.keys() & identified.comparison.frozen.keys():
        if observed.comparison.frozen[key] != identified.comparison.frozen[key]:
            return False
    known_metrics = {m.name: m for m in identified.metrics}
    for metric in observed.metrics:
        other = known_metrics.get(metric.name)
        if other and metric.value is not None and other.value is not None:
            if (metric.value, metric.unit) != (other.value, other.unit):
                return False
    return True


def _oracle_refines(observed: Oracle, identified: Oracle) -> bool:
    """Only a declared placeholder or compatible explicit components can refine.

    A reported verdict may still carry a fully explicit verifier identity. Its
    lower evidence authority does not permit replacing A with verifier B.
    """
    if observed.identity_status == "placeholder":
        return True
    if identified.identity_status != "explicit" or observed.kind != identified.kind:
        return False
    if observed.identity_components:
        return all(identified.identity_components.get(k) == v for k, v in observed.identity_components.items())
    return observed.oracle_id == identified.oracle_id


def _oracle_strength(oracle: Oracle) -> tuple[int, bool, int]:
    return ({"source_validated": 2, "reported": 1, "unavailable": 0}[oracle.authority],
            oracle.identity_status == "explicit", len(oracle.identity_components))
