"""Deterministic verification. The agent never treats its own message as proof.

Reports hash only the spec, the schema and the measured metrics, so running
the verifier twice against the same snapshot gives the same proof hash.
"""

from __future__ import annotations

from typing import Any

from .domain import Invariant, MigrationSpec, VerificationReport, Violation, sha256_hex


def _report(scope, spec: MigrationSpec, schema_hash: str, metrics: dict[str, Any], violations) -> VerificationReport:
    shown = {k: str(v) for k, v in sorted(metrics.items())}
    proof = sha256_hex({"scope": scope, "spec": spec.content_hash(), "schema": schema_hash, "metrics": shown})
    return VerificationReport(
        scope=scope,
        schema_hash=schema_hash,
        metrics=shown,
        violations=tuple(violations),
        proof_hash=proof,
        passed=not violations,
    )


def expand_report(spec: MigrationSpec, schema_hash: str, m: dict[str, Any]) -> VerificationReport:
    v: list[Violation] = []
    if m.get("new_column_type") != spec.change.new_type:
        v.append(Violation(invariant="new_column_type", observed=str(m.get("new_column_type")), expected=spec.change.new_type))
    if m.get("new_column_nullable") is not True:
        v.append(Violation(invariant="new_column_nullable", observed=str(m.get("new_column_nullable")), expected="True"))
    if not m.get("index_valid"):
        v.append(Violation(invariant=Invariant.INDEX_VALID.value, observed="False", expected="True"))
    if not m.get("dual_write_trigger"):
        v.append(Violation(invariant="dual_write_trigger", observed="False", expected="True"))
    return _report("expand", spec, schema_hash, m, v)


def backfill_report(
    spec: MigrationSpec,
    schema_hash: str,
    m: dict[str, Any],
    planned_upper: int | None,
    cursor: int | None,
    scope: str = "backfill",
) -> VerificationReport:
    inv = set(spec.invariants)
    v: list[Violation] = []
    if Invariant.NO_ORPHAN_SOURCES in inv and int(m["orphan_sources"]) != 0:
        v.append(Violation(invariant=Invariant.NO_ORPHAN_SOURCES.value, observed=str(m["orphan_sources"]), expected="0"))
    if Invariant.NO_NULL_TARGETS in inv and int(m["null_targets"]) != 0:
        v.append(Violation(invariant=Invariant.NO_NULL_TARGETS.value, observed=str(m["null_targets"]), expected="0"))
    if Invariant.MAPPING_PARITY in inv:
        if int(m["mismatches"]) != 0:
            v.append(Violation(invariant=Invariant.MAPPING_PARITY.value, observed=f"{m['mismatches']} rows differ", expected="0 rows differ"))
        elif m["source_checksum"] != m["target_checksum"]:
            v.append(Violation(invariant=Invariant.MAPPING_PARITY.value, observed=str(m["target_checksum"]), expected=str(m["source_checksum"])))
    if Invariant.NO_PENDING_CHUNKS in inv:
        pending = cursor is None or planned_upper is None or cursor <= planned_upper
        beyond = planned_upper is not None and int(m["max_key"]) > planned_upper
        if pending or beyond:
            v.append(
                Violation(
                    invariant=Invariant.NO_PENDING_CHUNKS.value,
                    observed=f"cursor={cursor} planned_upper={planned_upper} max_key={m['max_key']}",
                    expected="cursor past every key",
                )
            )
    if Invariant.INDEX_VALID in inv and not m.get("index_valid"):
        v.append(Violation(invariant=Invariant.INDEX_VALID.value, observed="False", expected="True"))
    metrics = {**m, "parity": m["source_checksum"] == m["target_checksum"]}
    return _report(scope, spec, schema_hash, metrics, v)


def contract_report(spec: MigrationSpec, schema_hash: str, m: dict[str, Any], expected_rows: int) -> VerificationReport:
    v: list[Violation] = []
    if spec.change.drop_old_column and m["old_column_present"]:
        v.append(Violation(invariant="old_column_dropped", observed="present", expected="absent"))
    if not m["new_column_not_null"]:
        v.append(Violation(invariant="new_column_not_null", observed="nullable", expected="NOT NULL"))
    if not m["fk_validated"]:
        v.append(Violation(invariant="fk_validated", observed="False", expected="True"))
    if int(m["rows_total"]) < expected_rows:
        v.append(Violation(invariant="row_count", observed=str(m["rows_total"]), expected=f">= {expected_rows}"))
    if int(m["rows_joined"]) != int(m["rows_total"]):
        v.append(Violation(invariant="smoke_join", observed=str(m["rows_joined"]), expected=str(m["rows_total"])))
    if m["dual_write_trigger"]:
        v.append(Violation(invariant="dual_write_removed", observed="present", expected="absent"))
    return _report("contract", spec, schema_hash, m, v)
