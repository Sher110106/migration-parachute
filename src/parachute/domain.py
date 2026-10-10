"""Domain contracts for Migration Parachute.

These models are the contract between the orchestrator, planner, adapters and
UI. They carry hashes, counts and redacted error classes, never row values or
secrets.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(value: Any) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(text.encode()).hexdigest()


def short_hash(value: Any) -> str:
    return sha256_hex(value)[:16]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _ident(value: str) -> str:
    if not IDENT_RE.match(value):
        raise ValueError(f"not a plain lower-case SQL identifier: {value!r}")
    return value


Ident = Annotated[str, Field(min_length=1, max_length=63)]


# --------------------------------------------------------------------------
# Phases, statuses, errors
# --------------------------------------------------------------------------


class Phase(str, enum.Enum):
    INIT = "INIT"
    PLANNED = "PLANNED"
    EXPAND_APPLY = "EXPAND_APPLY"
    EXPAND_VERIFY = "EXPAND_VERIFY"
    BACKFILL_PLAN = "BACKFILL_PLAN"
    BACKFILL_CHUNK_LOOP = "BACKFILL_CHUNK_LOOP"
    BACKFILL_VERIFY = "BACKFILL_VERIFY"
    CONTRACT_GUARD = "CONTRACT_GUARD"
    CONTRACT_READY = "CONTRACT_READY"
    CONTRACT_APPLY = "CONTRACT_APPLY"
    CONTRACT_VERIFY = "CONTRACT_VERIFY"
    COMPLETE = "COMPLETE"
    EXPAND_CLEANUP = "EXPAND_CLEANUP"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


TERMINAL_PHASES = {Phase.COMPLETE, Phase.ABORTED, Phase.FAILED, Phase.MANUAL_INTERVENTION}
PRE_CONTRACT_PHASES = {
    Phase.INIT,
    Phase.PLANNED,
    Phase.EXPAND_APPLY,
    Phase.EXPAND_VERIFY,
    Phase.BACKFILL_PLAN,
    Phase.BACKFILL_CHUNK_LOOP,
    Phase.BACKFILL_VERIFY,
    Phase.CONTRACT_GUARD,
    Phase.CONTRACT_READY,
}

# Forward edges of the state machine. Failure, cleanup and manual-intervention
# edges are added in ``allowed_transition`` below.
FORWARD_EDGES: dict[Phase, set[Phase]] = {
    Phase.INIT: {Phase.PLANNED},
    Phase.PLANNED: {Phase.EXPAND_APPLY, Phase.PLANNED},
    Phase.EXPAND_APPLY: {Phase.EXPAND_VERIFY},
    Phase.EXPAND_VERIFY: {Phase.BACKFILL_PLAN},
    Phase.BACKFILL_PLAN: {Phase.BACKFILL_CHUNK_LOOP},
    Phase.BACKFILL_CHUNK_LOOP: {Phase.BACKFILL_VERIFY},
    Phase.BACKFILL_VERIFY: {Phase.CONTRACT_GUARD, Phase.BACKFILL_CHUNK_LOOP},
    Phase.CONTRACT_GUARD: {Phase.CONTRACT_READY},
    Phase.CONTRACT_READY: {Phase.CONTRACT_APPLY},
    Phase.CONTRACT_APPLY: {Phase.CONTRACT_VERIFY},
    Phase.CONTRACT_VERIFY: {Phase.COMPLETE},
    Phase.EXPAND_CLEANUP: {Phase.ABORTED},
}


def allowed_transition(src: Phase, dst: Phase) -> bool:
    if src in TERMINAL_PHASES:
        return False
    if dst in FORWARD_EDGES.get(src, set()):
        return True
    if dst == Phase.FAILED:
        return True
    if dst == Phase.EXPAND_CLEANUP:
        return src in PRE_CONTRACT_PHASES
    if dst == Phase.MANUAL_INTERVENTION:
        return src in {Phase.CONTRACT_APPLY, Phase.CONTRACT_VERIFY}
    return False


class RunStatus(str, enum.Enum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class ErrorClass(str, enum.Enum):
    TRANSIENT = "TRANSIENT"
    LOCK_PRESSURE = "LOCK_PRESSURE"
    PROGRESS = "PROGRESS"
    FATAL_SCHEMA = "FATAL_SCHEMA"
    PERMISSION_POLICY = "PERMISSION_POLICY"
    PROOF_FAILURE = "PROOF_FAILURE"
    BUDGET = "BUDGET"
    STALE_SCHEMA = "STALE_SCHEMA"
    INVALID_SPEC = "INVALID_SPEC"
    UNKNOWN = "UNKNOWN"


RETRYABLE = {ErrorClass.TRANSIENT, ErrorClass.LOCK_PRESSURE}


class Blocker(Frozen):
    pid: int
    lock_mode: str
    granted: bool
    state: str | None = None
    xact_age_ms: int | None = None
    query_hash: str | None = None


class ErrorEnvelope(Frozen):
    error_class: ErrorClass
    sqlstate: str | None = None
    message: str
    retryable: bool = False
    blockers: tuple[Blocker, ...] = ()
    operator_action: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Migration spec
# --------------------------------------------------------------------------


class LookupBackfillChange(Frozen):
    """Replace a denormalised text column with a foreign key found by lookup.

    Example: ``orders.customer_email`` becomes ``orders.customer_id`` by
    matching ``customers.email`` and taking ``customers.id``.
    """

    kind: Literal["lookup_backfill"] = "lookup_backfill"
    old_column: Ident
    new_column: Ident
    new_type: Literal["bigint", "integer"] = "bigint"
    lookup_table: Ident
    lookup_match_column: Ident
    lookup_value_column: Ident
    add_foreign_key: bool = True
    drop_old_column: bool = True

    _check = field_validator(
        "old_column", "new_column", "lookup_table", "lookup_match_column", "lookup_value_column"
    )(_ident)


CanonicalChange = Annotated[Union[LookupBackfillChange], Field(discriminator="kind")]


class Invariant(str, enum.Enum):
    NO_NULL_TARGETS = "no_null_targets"
    MAPPING_PARITY = "mapping_parity"
    NO_ORPHAN_SOURCES = "no_orphan_sources"
    NO_PENDING_CHUNKS = "no_pending_chunks"
    INDEX_VALID = "index_valid"


class ChunkPolicy(Frozen):
    initial_batch: int = Field(2000, ge=1)
    min_batch: int = Field(250, ge=1)
    max_batch: int = Field(5000, ge=1)
    shrink_factor: int = Field(4, ge=2)
    grow_after_successes: int = Field(4, ge=1)
    base_backoff_ms: int = Field(400, ge=0)
    max_backoff_ms: int = Field(5000, ge=0)
    catch_up_row_limit: int = Field(5000, ge=0)

    @model_validator(mode="after")
    def _bounds(self) -> "ChunkPolicy":
        if not (self.min_batch <= self.initial_batch <= self.max_batch):
            raise ValueError("chunk policy needs min_batch <= initial_batch <= max_batch")
        return self


class ApprovalPolicy(Frozen):
    require_owner_approval: bool = True
    owner: str = "data-platform-owner"


class MigrationSpec(Frozen):
    migration_id: Ident
    database_fingerprint: str
    target_table: Ident
    primary_key: Ident
    change: CanonicalChange
    invariants: tuple[Invariant, ...] = tuple(Invariant)
    lock_budget_ms: int = Field(300, ge=10, le=60_000)
    statement_timeout_ms: int = Field(5000, ge=100, le=600_000)
    max_retries: int = Field(5, ge=0, le=50)
    chunk_policy: ChunkPolicy = ChunkPolicy()
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    cost_cap_usd: Decimal = Field(Decimal("0.50"), ge=0)

    _check = field_validator("migration_id", "target_table", "primary_key")(_ident)

    def spec_hash(self) -> str:
        return sha256_hex(self)

    def content_hash(self) -> str:
        """The spec without the database identity, for comparing proofs across fixture resets."""
        return sha256_hex(self.model_dump(mode="json", exclude={"database_fingerprint"}))


# --------------------------------------------------------------------------
# Schema, plan, tools, verification
# --------------------------------------------------------------------------


class ColumnInfo(Frozen):
    name: str
    data_type: str
    not_null: bool
    default: str | None = None


class SchemaSnapshot(Frozen):
    table: str
    columns: tuple[ColumnInfo, ...]
    indexes: tuple[str, ...]
    constraints: tuple[str, ...]
    triggers: tuple[str, ...]
    schema_hash: str
    row_count: int
    min_key: int | None
    max_key: int | None
    key_unique_not_null: bool
    lookup_unique: bool

    def column(self, name: str) -> ColumnInfo | None:
        return next((c for c in self.columns if c.name == name), None)


class SqlArtifact(Frozen):
    name: str
    statements: tuple[str, ...]
    tx_mode: Literal["transactional", "autocommit"] = "transactional"

    @property
    def sql_hash(self) -> str:
        return sha256_hex("\n".join(self.statements))


class MigrationPlan(Frozen):
    baseline_schema_hash: str
    expand: tuple[SqlArtifact, ...]
    backfill_template_hash: str
    contract: tuple[SqlArtifact, ...]
    rollback_expand: tuple[SqlArtifact, ...]
    key_lower: int
    key_upper: int
    estimated_rows: int
    initial_batch: int
    estimated_chunks: int
    summary: str

    def plan_hash(self) -> str:
        return sha256_hex(self.model_dump(mode="json", exclude={"summary"}))


class ToolCall(Frozen):
    tool: str
    idempotency_key: str
    run_id: str
    phase: Phase
    attempt: int
    deadline: datetime
    tx_mode: Literal["transactional", "autocommit"] = "transactional"
    params: dict[str, Any] = Field(default_factory=dict)


class ToolResult(Frozen):
    ok: bool
    sql_hash: str | None = None
    rows: int | None = None
    checksum: str | None = None
    started_at: datetime
    ended_at: datetime
    lock_wait_ms: int = 0
    reconciled: bool = False
    value: dict[str, Any] = Field(default_factory=dict)
    error: ErrorEnvelope | None = None

    @property
    def duration_ms(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds() * 1000)


class Violation(Frozen):
    invariant: str
    observed: str
    expected: str


class VerificationReport(Frozen):
    scope: Literal["expand", "backfill", "contract_guard", "contract"]
    schema_hash: str
    metrics: dict[str, str]
    violations: tuple[Violation, ...]
    proof_hash: str
    passed: bool


class ChunkRecord(Frozen):
    lower: int
    upper: int
    rows: int
    checksum: str
    idempotency_key: str
    attempts: int
    duration_ms: int
    reconciled: bool = False


# --------------------------------------------------------------------------
# Planner decisions
# --------------------------------------------------------------------------


class ActionKind(str, enum.Enum):
    INSPECT = "inspect"
    CREATE_PLAN = "create_plan"
    REPLAN = "replan"
    APPLY_EXPAND = "apply_expand"
    VERIFY_EXPAND = "verify_expand"
    PLAN_BACKFILL = "plan_backfill"
    RUN_CHUNK = "run_chunk"
    RETRY_CHUNK = "retry_chunk"
    RETRY_STEP = "retry_step"
    CATCH_UP = "catch_up"
    VERIFY_BACKFILL = "verify_backfill"
    PREPARE_CONTRACT = "prepare_contract"
    APPLY_CONTRACT = "apply_contract"
    VERIFY_CONTRACT = "verify_contract"
    PAUSE = "pause"
    ABORT = "abort"
    FAIL = "fail"


class ActionOption(Frozen):
    """One action the policy permits now, with bounds on its parameters."""

    kind: ActionKind
    batch_size_range: tuple[int, int] | None = None
    backoff_ms_range: tuple[int, int] | None = None
    note: str = ""


class Decision(Frozen):
    kind: ActionKind
    batch_size: int | None = None
    backoff_ms: int | None = None
    summary: str
    planner: str
    tokens_in: int = 0
    tokens_out: int = 0


class Observation(Frozen):
    run_id: str
    phase: Phase
    cursor: int | None
    key_upper: int | None
    batch_size: int
    retry_count: int
    max_retries: int
    consecutive_successes: int
    last_error: ErrorEnvelope | None
    rows_backfilled: int
    cost_usd: str
    cost_cap_usd: str
    options: tuple[ActionOption, ...]


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


class EventBase(Frozen):
    at: datetime = Field(default_factory=utcnow)


class RunStarted(EventBase):
    type: Literal["run_started"] = "run_started"
    run_id: str
    spec: MigrationSpec
    spec_hash: str
    mode: str = "parachute"
    planner: str = ""


class LeaseAcquired(EventBase):
    type: Literal["lease_acquired"] = "lease_acquired"
    owner: str
    fence_token: int


class SchemaInspected(EventBase):
    type: Literal["schema_inspected"] = "schema_inspected"
    snapshot: SchemaSnapshot
    purpose: Literal["baseline", "drift_check", "post_expand", "post_contract"] = "baseline"


class PlanCreated(EventBase):
    type: Literal["plan_created"] = "plan_created"
    plan: MigrationPlan
    plan_hash: str
    planner: str


class PhaseChanged(EventBase):
    type: Literal["phase_changed"] = "phase_changed"
    from_phase: Phase
    to_phase: Phase
    reason: str = ""


class StepRecorded(EventBase):
    type: Literal["step_recorded"] = "step_recorded"
    tool: str
    idempotency_key: str
    attempt: int
    ok: bool
    sql_hash: str | None = None
    rows: int | None = None
    duration_ms: int = 0
    lock_wait_ms: int = 0
    reconciled: bool = False
    error: ErrorEnvelope | None = None


class BackfillPlanned(EventBase):
    type: Literal["backfill_planned"] = "backfill_planned"
    lower: int
    upper: int
    batch_size: int


class ChunkCommitted(EventBase):
    type: Literal["chunk_committed"] = "chunk_committed"
    chunk: ChunkRecord


class ChunkFailed(EventBase):
    type: Literal["chunk_failed"] = "chunk_failed"
    lower: int
    upper: int
    batch_size: int
    attempt: int
    error: ErrorEnvelope
    lock_wait_ms: int = 0


class DecisionMade(EventBase):
    type: Literal["decision_made"] = "decision_made"
    decision: Decision
    accepted: bool
    rejection: str | None = None
    forced: bool = False


class CatchUpPlanned(EventBase):
    type: Literal["catch_up_planned"] = "catch_up_planned"
    previous_upper: int
    new_upper: int
    new_rows: int


class VerificationRecorded(EventBase):
    type: Literal["verification_recorded"] = "verification_recorded"
    report: VerificationReport


class ContractPrepared(EventBase):
    type: Literal["contract_prepared"] = "contract_prepared"
    sql_hash: str
    statements: tuple[str, ...]
    rollback_statements: tuple[str, ...]
    dependencies: tuple[str, ...]
    proof_hash: str
    approval_id: str


class ApprovalGranted(EventBase):
    type: Literal["approval_granted"] = "approval_granted"
    approval_id: str
    approver: str
    sql_hash: str


class CostRecorded(EventBase):
    type: Literal["cost_recorded"] = "cost_recorded"
    phase: Phase
    tokens_in: int = 0
    tokens_out: int = 0
    db_ms: int = 0
    lock_wait_ms: int = 0
    rows: int = 0
    cost_usd: Decimal = Decimal("0")


class RunPaused(EventBase):
    type: Literal["run_paused"] = "run_paused"
    reason: Literal["budget", "lock_budget", "approval", "operator", "drift"]
    detail: str = ""


class RunResumed(EventBase):
    type: Literal["run_resumed"] = "run_resumed"
    reason: str = ""
    new_cost_cap_usd: Decimal | None = None


class RunFailed(EventBase):
    type: Literal["run_failed"] = "run_failed"
    error: ErrorEnvelope


class RunCompleted(EventBase):
    type: Literal["run_completed"] = "run_completed"
    proof_hash: str


class RunAborted(EventBase):
    type: Literal["run_aborted"] = "run_aborted"
    reason: str


Event = Annotated[
    Union[
        RunStarted,
        LeaseAcquired,
        SchemaInspected,
        PlanCreated,
        PhaseChanged,
        StepRecorded,
        BackfillPlanned,
        ChunkCommitted,
        ChunkFailed,
        DecisionMade,
        CatchUpPlanned,
        VerificationRecorded,
        ContractPrepared,
        ApprovalGranted,
        CostRecorded,
        RunPaused,
        RunResumed,
        RunFailed,
        RunCompleted,
        RunAborted,
    ],
    Field(discriminator="type"),
]


class EventEnvelope(BaseModel):
    """A stored event with its position in the hash chain."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    seq: int
    event: Event
    payload_hash: str
    prev_hash: str
