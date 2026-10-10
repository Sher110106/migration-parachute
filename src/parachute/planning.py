"""Turn a spec and a schema snapshot into a bounded migration plan."""

from __future__ import annotations

import math

from .domain import ErrorClass, ErrorEnvelope, MigrationPlan, MigrationSpec, SchemaSnapshot
from .sqlgen import (
    Names,
    backfill_template_hash,
    contract_artifacts,
    expand_artifacts,
    rollback_expand_artifacts,
)


def spec_problems(spec: MigrationSpec, snap: SchemaSnapshot, fingerprint: str) -> ErrorEnvelope | None:
    """Reject a spec before any side effect."""
    n = Names.of(spec)
    if spec.database_fingerprint != fingerprint:
        return ErrorEnvelope(
            error_class=ErrorClass.INVALID_SPEC,
            message=f"spec targets {spec.database_fingerprint}, connected to {fingerprint}",
            operator_action="Point the run at the database the spec was written for.",
        )
    if not snap.key_unique_not_null:
        return ErrorEnvelope(
            error_class=ErrorClass.INVALID_SPEC,
            message=f"{n.table}.{n.pk} is not a unique, non-null integer key",
            operator_action="Chunking needs a stable unique key. Choose another key column.",
        )
    if not snap.lookup_unique:
        return ErrorEnvelope(
            error_class=ErrorClass.INVALID_SPEC,
            message=f"{n.lookup_table}.{n.lookup_match} is not unique, so the lookup is ambiguous",
            operator_action="Add a unique index on the lookup column first.",
        )
    if snap.column(n.old) is None:
        return ErrorEnvelope(
            error_class=ErrorClass.INVALID_SPEC,
            message=f"{n.table}.{n.old} does not exist",
            operator_action="Check the source column name.",
        )
    return column_conflict(spec, snap)


def column_conflict(spec: MigrationSpec, snap: SchemaSnapshot) -> ErrorEnvelope | None:
    n = Names.of(spec)
    existing = snap.column(n.new)
    if existing is not None and existing.data_type != spec.change.new_type:
        return ErrorEnvelope(
            error_class=ErrorClass.FATAL_SCHEMA,
            message=f"{n.table}.{n.new} already exists as {existing.data_type}, plan needs {spec.change.new_type}",
            operator_action="Someone else owns this column. Resolve the conflict by hand before re-running.",
        )
    return None


def build_plan(spec: MigrationSpec, snap: SchemaSnapshot) -> MigrationPlan:
    lower = snap.min_key if snap.min_key is not None else 1
    upper = snap.max_key if snap.max_key is not None else 0
    batch = spec.chunk_policy.initial_batch
    rows = snap.row_count
    chunks = max(0, math.ceil((upper - lower + 1) / batch)) if upper >= lower else 0
    n = Names.of(spec)
    summary = (
        f"Add nullable {n.table}.{n.new}, dual-write it from {n.old}, build {n.index} concurrently; "
        f"backfill {rows:,} rows in about {chunks} chunks of {batch:,} by {n.pk}; "
        f"verify parity; then stage the contract behind owner approval."
    )
    return MigrationPlan(
        baseline_schema_hash=snap.schema_hash,
        expand=expand_artifacts(spec),
        backfill_template_hash=backfill_template_hash(spec),
        contract=contract_artifacts(spec),
        rollback_expand=rollback_expand_artifacts(spec),
        key_lower=lower,
        key_upper=upper,
        estimated_rows=rows,
        initial_batch=batch,
        estimated_chunks=chunks,
        summary=summary,
    )
