"""Materialized run state and the pure reducer that builds it from events.

The event log is the source of truth. ``replay(events)`` must give the same
state on every machine, which is what makes crash recovery and audit work.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    ApprovalGranted,
    BackfillPlanned,
    CatchUpPlanned,
    ChunkCommitted,
    ChunkFailed,
    ChunkRecord,
    ContractPrepared,
    CostRecorded,
    Decision,
    DecisionMade,
    ErrorEnvelope,
    Event,
    LeaseAcquired,
    MigrationPlan,
    MigrationSpec,
    Phase,
    PhaseChanged,
    PlanCreated,
    RunAborted,
    RunCompleted,
    RunFailed,
    RunPaused,
    RunResumed,
    RunStarted,
    RunStatus,
    SchemaInspected,
    SchemaSnapshot,
    StepRecorded,
    TERMINAL_PHASES,
    VerificationReport,
    VerificationRecorded,
    allowed_transition,
)


class IllegalTransition(Exception):
    pass


class RunState(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    spec: MigrationSpec
    spec_hash: str
    mode: str
    planner: str = ""
    phase: Phase = Phase.INIT
    status: RunStatus = RunStatus.RUNNING
    revision: int = 0
    fence_token: int = 0
    owner: str | None = None

    baseline: SchemaSnapshot | None = None
    current_schema_hash: str | None = None
    plan: MigrationPlan | None = None
    plan_hash: str | None = None
    replans: int = 0

    cursor: int | None = None
    key_upper: int | None = None
    batch_size: int = 0
    chunks: tuple[ChunkRecord, ...] = ()
    rows_backfilled: int = 0
    retry_count: int = 0
    total_retries: int = 0
    consecutive_successes: int = 0
    catch_ups: int = 0

    steps: int = 0
    failed_steps: int = 0
    reconciled_steps: int = 0
    decisions: int = 0
    rejected_decisions: int = 0
    last_decision: Decision | None = None
    last_error: ErrorEnvelope | None = None

    verifications: dict[str, VerificationReport] = Field(default_factory=dict)

    contract_sql_hash: str | None = None
    contract_statements: tuple[str, ...] = ()
    rollback_statements: tuple[str, ...] = ()
    contract_dependencies: tuple[str, ...] = ()
    contract_proof_hash: str | None = None
    approval_id: str | None = None
    approved_by: str | None = None

    tokens_in: int = 0
    tokens_out: int = 0
    db_ms: int = 0
    lock_wait_ms: int = 0
    cost_usd: Decimal = Decimal("0")
    cost_cap_usd: Decimal = Decimal("0")

    pause_reason: str | None = None
    pause_detail: str | None = None
    abort_reason: str | None = None
    final_proof_hash: str | None = None
    started_at: str | None = None
    updated_at: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def backfill_done(self) -> bool:
        return (
            self.cursor is not None
            and self.key_upper is not None
            and self.cursor > self.key_upper
        )

    @property
    def display_state(self) -> str:
        if self.status == RunStatus.PAUSED and self.pause_reason:
            return f"PAUSED_{self.pause_reason.upper()}"
        return self.phase.value


def _phase(state: RunState, dst: Phase) -> dict:
    if not allowed_transition(state.phase, dst):
        raise IllegalTransition(f"{state.phase.value} -> {dst.value} is not allowed")
    status = state.status
    if dst == Phase.CONTRACT_READY:
        status = RunStatus.AWAITING_APPROVAL
    elif dst == Phase.MANUAL_INTERVENTION:
        status = RunStatus.MANUAL_INTERVENTION
    elif state.phase == Phase.CONTRACT_READY:
        status = RunStatus.RUNNING
    return {"phase": dst, "status": status}


def apply(state: RunState | None, event: Event) -> RunState:
    if isinstance(event, RunStarted):
        if state is not None:
            raise IllegalTransition("run already started")
        return RunState(
            run_id=event.run_id,
            spec=event.spec,
            spec_hash=event.spec_hash,
            mode=event.mode,
            planner=event.planner,
            cost_cap_usd=event.spec.cost_cap_usd,
            batch_size=event.spec.chunk_policy.initial_batch,
            revision=1,
            started_at=event.at.isoformat(),
            updated_at=event.at.isoformat(),
        )
    if state is None:
        raise IllegalTransition(f"{event.type} before run_started")
    if state.terminal and not isinstance(event, (CostRecorded, StepRecorded)):
        raise IllegalTransition(f"{event.type} after terminal phase {state.phase.value}")

    u: dict = {"revision": state.revision + 1, "updated_at": event.at.isoformat()}

    if isinstance(event, LeaseAcquired):
        if event.fence_token <= state.fence_token:
            raise IllegalTransition("fence token must increase")
        u.update(fence_token=event.fence_token, owner=event.owner)
    elif isinstance(event, SchemaInspected):
        u["current_schema_hash"] = event.snapshot.schema_hash
        if event.purpose == "baseline":
            u["baseline"] = event.snapshot
    elif isinstance(event, PlanCreated):
        u.update(
            plan=event.plan,
            plan_hash=event.plan_hash,
            replans=state.replans + (1 if state.plan is not None else 0),
            batch_size=event.plan.initial_batch,
        )
    elif isinstance(event, PhaseChanged):
        if event.from_phase != state.phase:
            raise IllegalTransition(
                f"event says {event.from_phase.value} but state is {state.phase.value}"
            )
        u.update(_phase(state, event.to_phase))
    elif isinstance(event, StepRecorded):
        u.update(
            steps=state.steps + 1,
            failed_steps=state.failed_steps + (0 if event.ok else 1),
            reconciled_steps=state.reconciled_steps + (1 if event.reconciled else 0),
        )
    elif isinstance(event, BackfillPlanned):
        u.update(cursor=event.lower, key_upper=event.upper, batch_size=event.batch_size)
    elif isinstance(event, ChunkCommitted):
        c = event.chunk
        if state.cursor is not None and c.lower != state.cursor:
            raise IllegalTransition(f"chunk starts at {c.lower}, cursor is {state.cursor}")
        u.update(
            chunks=state.chunks + (c,),
            cursor=c.upper + 1,
            rows_backfilled=state.rows_backfilled + c.rows,
            retry_count=0,
            consecutive_successes=state.consecutive_successes + 1,
            last_error=None,
        )
    elif isinstance(event, ChunkFailed):
        u.update(
            retry_count=state.retry_count + 1,
            total_retries=state.total_retries + 1,
            consecutive_successes=0,
            last_error=event.error,
        )
    elif isinstance(event, DecisionMade):
        u.update(
            decisions=state.decisions + 1,
            rejected_decisions=state.rejected_decisions + (0 if event.accepted else 1),
            last_decision=event.decision,
        )
        if event.accepted and event.decision.batch_size:
            u["batch_size"] = event.decision.batch_size
            if event.decision.batch_size > state.batch_size:
                u["consecutive_successes"] = 0
    elif isinstance(event, CatchUpPlanned):
        u.update(key_upper=event.new_upper, catch_ups=state.catch_ups + 1)
    elif isinstance(event, VerificationRecorded):
        u["verifications"] = {**state.verifications, event.report.scope: event.report}
        u["current_schema_hash"] = event.report.schema_hash
    elif isinstance(event, ContractPrepared):
        u.update(
            contract_sql_hash=event.sql_hash,
            contract_statements=event.statements,
            rollback_statements=event.rollback_statements,
            contract_dependencies=event.dependencies,
            contract_proof_hash=event.proof_hash,
            approval_id=event.approval_id,
        )
    elif isinstance(event, ApprovalGranted):
        if event.approval_id != state.approval_id or event.sql_hash != state.contract_sql_hash:
            raise IllegalTransition("approval does not match the prepared contract")
        u["approved_by"] = event.approver
    elif isinstance(event, CostRecorded):
        u.update(
            tokens_in=state.tokens_in + event.tokens_in,
            tokens_out=state.tokens_out + event.tokens_out,
            db_ms=state.db_ms + event.db_ms,
            lock_wait_ms=state.lock_wait_ms + event.lock_wait_ms,
            cost_usd=state.cost_usd + event.cost_usd,
        )
    elif isinstance(event, RunPaused):
        u.update(status=RunStatus.PAUSED, pause_reason=event.reason, pause_detail=event.detail)
    elif isinstance(event, RunResumed):
        if state.status != RunStatus.PAUSED:
            raise IllegalTransition("only a paused run can resume")
        status = RunStatus.AWAITING_APPROVAL if state.phase == Phase.CONTRACT_READY else RunStatus.RUNNING
        u.update(status=status, pause_reason=None, pause_detail=None, retry_count=0)
        if event.new_cost_cap_usd is not None:
            u["cost_cap_usd"] = event.new_cost_cap_usd
    elif isinstance(event, RunFailed):
        u.update(_phase(state, Phase.FAILED))
        u.update(status=RunStatus.FAILED, last_error=event.error)
    elif isinstance(event, RunCompleted):
        u.update(_phase(state, Phase.COMPLETE))
        u.update(status=RunStatus.COMPLETE, final_proof_hash=event.proof_hash)
    elif isinstance(event, RunAborted):
        u.update(_phase(state, Phase.ABORTED))
        u.update(status=RunStatus.ABORTED, pause_reason=None, abort_reason=event.reason)
    else:  # pragma: no cover - the union is closed
        raise IllegalTransition(f"unknown event {event!r}")

    return state.model_copy(update=u)


def replay(events: Iterable[Event]) -> RunState:
    state: RunState | None = None
    for event in events:
        state = apply(state, event)
    if state is None:
        raise ValueError("no events")
    return state
