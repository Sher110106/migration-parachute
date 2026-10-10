"""The migration orchestrator: observe, decide, act, verify, persist, repeat.

Callers use five methods: start_run, resume_run, approve_contract,
abort_run and get_run. Everything else (planner, policy, adapter, verifier,
ledger, traces, costs) sits behind them.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Callable

from . import policy, verifier
from .domain import (
    ActionKind,
    ActionOption,
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
    ErrorClass,
    ErrorEnvelope,
    Event,
    MigrationSpec,
    Observation,
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
    SqlArtifact,
    StepRecorded,
    ToolCall,
    ToolResult,
    VerificationRecorded,
    sha256_hex,
    short_hash,
    utcnow,
)
from .economics import CostModel
from .faults import FaultHooks, NoFaults
from .pg import PostgresAdapter, chunk_key, step_key
from .planners import AdaptivePlanner, Planner
from .planning import build_plan, column_conflict, spec_problems
from .state import RunState
from .store import ControlStore
from .tracing import NullTrace

MAX_STEPS = 5_000
MAX_REPLANS = 3


@dataclass
class Services:
    store: ControlStore
    db: PostgresAdapter
    planner: Planner
    tracer: Any = field(default_factory=NullTrace)
    faults: FaultHooks = field(default_factory=NoFaults)
    costs: CostModel = field(default_factory=CostModel)
    sleep: Callable[[float], None] = time.sleep
    fallback: Planner | None = field(default_factory=AdaptivePlanner)


class Orchestrator:
    mode = "parachute"

    def __init__(self, services: Services, owner: str | None = None):
        self.s = services
        self.owner = owner or f"worker-{uuid.uuid4().hex[:6]}"
        self.state: RunState | None = None
        self.fence = 0
        self._fingerprint: str | None = None

    # ================================================================ API

    def start_run(self, spec: MigrationSpec, run_id: str | None = None) -> RunState:
        run_id = run_id or f"run_{uuid.uuid4().hex[:10]}"
        self.s.store.create_run(
            RunStarted(run_id=run_id, spec=spec, spec_hash=spec.spec_hash(), mode=self.mode, planner=self.s.planner.name)
        )
        return self._drive(run_id, entry="migration_run")

    def resume_run(self, run_id: str, new_cost_cap_usd: Decimal | None = None, reason: str = "operator resume") -> RunState:
        state = self.s.store.load(run_id)
        if state.terminal:
            return state
        if state.status == RunStatus.PAUSED:
            self._lease(run_id)
            self._emit(RunResumed(reason=reason, new_cost_cap_usd=new_cost_cap_usd))
        return self._drive(run_id, entry="migration_resume")

    def approve_contract(self, run_id: str, approval_id: str, approver: str) -> RunState:
        state = self.s.store.load(run_id)
        if state.phase != Phase.CONTRACT_READY or state.approval_id != approval_id:
            raise ValueError("no prepared contract with that approval id")
        self._lease(run_id)
        self._emit(ApprovalGranted(approval_id=approval_id, approver=approver, sql_hash=state.contract_sql_hash))
        return self._drive(run_id, entry="contract_approval")

    def abort_run(self, run_id: str, reason: str = "operator abort") -> RunState:
        self._lease(run_id)
        try:
            with self.s.db.run_lock(run_id), self.s.tracer.span(
                "WORKFLOW", "migration_abort", run_id=run_id, mode=self.mode, reason=reason
            ) as root:
                self._abort(reason)
                root.set(final_state=self.state.display_state)
        finally:
            self.s.tracer.flush()
        return self.state

    def get_run(self, run_id: str) -> RunState:
        return self.s.store.load(run_id)

    # ========================================================== plumbing

    @property
    def spec(self) -> MigrationSpec:
        return self.state.spec

    @property
    def fingerprint(self) -> str:
        if self._fingerprint is None:
            self._fingerprint = self.s.db.fingerprint()
        return self._fingerprint

    def _lease(self, run_id: str) -> None:
        self.state = self.s.store.acquire_lease(run_id, self.owner)
        self.fence = self.state.fence_token

    def _emit(self, *events: Event) -> RunState:
        self.state = self.s.store.append(self.state.run_id, list(events), self.state.revision, self.fence)
        return self.state

    def _drive(self, run_id: str, entry: str = "migration_run") -> RunState:
        self._lease(run_id)
        st = self.state
        try:
            self._drive_locked(run_id, st, entry)
        finally:
            self.s.tracer.flush()
        return self.state

    def _drive_locked(self, run_id: str, st: RunState, entry: str) -> None:
        with self.s.db.run_lock(run_id), self.s.tracer.span(
            "WORKFLOW",
            entry,
            run_id=run_id,
            mode=self.mode,
            migration_id=st.spec.migration_id,
            planner=self.s.planner.name,
            fixture="fictional local fixture",
            entry_phase=st.phase.value,
        ) as root:
            self._reconcile_on_entry()
            steps = 0
            while self._runnable():
                steps += 1
                if steps > MAX_STEPS:
                    raise RuntimeError("step limit reached; refusing to loop forever")
                self._step()
            root.set(
                final_state=self.state.display_state,
                rows_backfilled=self.state.rows_backfilled,
                retries=self.state.total_retries,
                lock_wait_ms=self.state.lock_wait_ms,
                cost_usd=str(self.state.cost_usd),
                checkpoint_ref=self.s.store.latest_checkpoint_ref(run_id),
            )

    def _runnable(self) -> bool:
        st = self.state
        if st.terminal or st.status == RunStatus.PAUSED:
            return False
        if st.status == RunStatus.AWAITING_APPROVAL and not st.approved_by:
            return False
        return True

    def _step(self) -> None:
        handlers = {
            Phase.INIT: self._init,
            Phase.PLANNED: self._planned,
            Phase.EXPAND_APPLY: self._expand_apply,
            Phase.EXPAND_VERIFY: self._expand_verify,
            Phase.BACKFILL_PLAN: self._backfill_plan,
            Phase.BACKFILL_CHUNK_LOOP: self._backfill_loop,
            Phase.BACKFILL_VERIFY: self._backfill_verify,
            Phase.CONTRACT_GUARD: self._contract_guard,
            Phase.CONTRACT_READY: self._contract_ready,
            Phase.CONTRACT_APPLY: self._contract_apply,
            Phase.CONTRACT_VERIFY: self._contract_verify,
            Phase.EXPAND_CLEANUP: lambda: self._abort("resume cleanup"),
        }
        handlers[self.state.phase]()

    def _goto(self, dst: Phase, reason: str = "", *more: Event) -> None:
        self._emit(PhaseChanged(from_phase=self.state.phase, to_phase=dst, reason=reason), *more)

    def _fail(self, error: ErrorEnvelope) -> None:
        with self.s.tracer.span("GUARDRAIL", "fail_closed", phase=self.state.phase.value) as sp:
            sp.set(passed=False, reason=error.message, error_class=error.error_class.value)
        self._emit(RunFailed(error=error))

    def _pause(self, reason: str, detail: str) -> None:
        with self.s.tracer.span("GUARDRAIL", "pause", phase=self.state.phase.value) as sp:
            sp.set(passed=False, reason=f"{reason}: {detail}")
        self._emit(RunPaused(reason=reason, detail=detail))

    def _cost_event(self, result: ToolResult | None = None, tokens_in: int = 0, tokens_out: int = 0, rows: int = 0) -> CostRecorded:
        db_ms = result.duration_ms if result else 0
        return CostRecorded(
            phase=self.state.phase,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            db_ms=db_ms,
            lock_wait_ms=result.lock_wait_ms if result else 0,
            rows=rows,
            cost_usd=self.s.costs.cost(tokens_in, tokens_out, db_ms),
        )

    def _over_budget(self) -> bool:
        st = self.state
        if st.cost_usd >= st.cost_cap_usd:
            self._pause("budget", f"spent ${st.cost_usd} of ${st.cost_cap_usd} cap; checkpoint kept")
            return True
        return False

    def _call(self, tool: str, key: str, attempt: int, tx_mode: str = "transactional", **params) -> ToolCall:
        return ToolCall(
            tool=tool,
            idempotency_key=key,
            run_id=self.state.run_id,
            phase=self.state.phase,
            attempt=attempt,
            deadline=utcnow() + timedelta(milliseconds=self.spec.statement_timeout_ms),
            tx_mode=tx_mode,
            params=params,
        )

    def _observation(self, options: tuple[ActionOption, ...]) -> Observation:
        st = self.state
        return Observation(
            run_id=st.run_id,
            phase=st.phase,
            cursor=st.cursor,
            key_upper=st.key_upper,
            batch_size=st.batch_size,
            retry_count=st.retry_count,
            max_retries=st.spec.max_retries,
            consecutive_successes=st.consecutive_successes,
            last_error=st.last_error,
            rows_backfilled=st.rows_backfilled,
            cost_usd=str(st.cost_usd),
            cost_cap_usd=str(st.cost_cap_usd),
            options=options,
        )

    def _decide(self, options: tuple[ActionOption, ...], obs: Observation | None = None) -> Decision:
        """Ask the planner, check its answer against policy, record both."""
        obs = obs or self._observation(options)
        with self.s.tracer.span("AGENT", "planner.decide", planner=self.s.planner.name, phase=obs.phase.value) as sp:
            rejection = None
            try:
                decision = self.s.planner.decide(obs)
                rejection = policy.validate(decision, options)
            except Exception as exc:  # a planner failure must not stop the run unsafely
                decision = Decision(kind=ActionKind.PAUSE, summary="planner error", planner=self.s.planner.name)
                rejection = f"planner raised {type(exc).__name__}: {str(exc)[:160]}"
            events: list[Event] = []
            if decision.tokens_in or decision.tokens_out:
                events.append(self._cost_event(tokens_in=decision.tokens_in, tokens_out=decision.tokens_out))
            if rejection:
                events.append(DecisionMade(decision=decision, accepted=False, rejection=rejection))
                decision = self._fallback_decision(obs, options, rejection)
            events.append(DecisionMade(decision=decision, accepted=True, forced=decision.planner == "policy"))
            self._emit(*events)
            sp.set(
                action=decision.kind.value,
                batch_size=decision.batch_size,
                backoff_ms=decision.backoff_ms,
                summary=decision.summary,
                rejected=rejection or "",
                tokens_in=decision.tokens_in,
                tokens_out=decision.tokens_out,
            )
        return decision

    def _fallback_decision(self, obs: Observation, options: tuple[ActionOption, ...], rejection: str) -> Decision:
        """After a rejected answer: ask the deterministic planner, else pause."""
        fb = self.s.fallback
        if fb is not None and fb is not self.s.planner:
            try:
                alt = fb.decide(obs)
                if policy.validate(alt, options) is None:
                    return alt.model_copy(update={"summary": f"Planner answer rejected ({rejection}). {alt.summary}"})
            except Exception:
                pass
        safe = next((o for o in options if o.kind == ActionKind.PAUSE), options[0])
        return policy.forced_decision(safe, f"Planner answer rejected ({rejection}); policy chose {safe.kind.value}.")

    def _inspect(self, purpose: str):
        with self.s.tracer.span("TOOL", "inspect_schema", phase=self.state.phase.value, purpose=purpose) as sp:
            snap = self.s.db.inspect(self.spec)
            sp.set(schema_hash=snap.schema_hash[:16], rows=snap.row_count, max_key=snap.max_key)
        return snap

    def _reconcile_on_entry(self) -> None:
        """After a crash, adopt chunks the database committed but we never recorded."""
        st = self.state
        if st.phase != Phase.BACKFILL_CHUNK_LOOP or st.cursor is None:
            return
        entries = self.s.db.ledger_chunks(st.spec.migration_id, st.cursor)
        with self.s.tracer.span("TOOL", "reconcile_ledger", cursor=st.cursor) as sp:
            adopted = 0
            for e in entries:
                if e["lower"] != self.state.cursor:
                    break
                chunk = ChunkRecord(
                    lower=e["lower"],
                    upper=e["upper"],
                    rows=e["rows"],
                    checksum=e["checksum"],
                    idempotency_key=e["idempotency_key"],
                    attempts=1,
                    duration_ms=0,
                    reconciled=True,
                )
                self._emit(
                    StepRecorded(tool="reconcile_ledger", idempotency_key=e["idempotency_key"], attempt=1, ok=True, rows=e["rows"], reconciled=True),
                    ChunkCommitted(chunk=chunk),
                )
                adopted += 1
            sp.set(adopted_chunks=adopted, cursor_after=self.state.cursor)

    # ========================================================== phases

    def _init(self) -> None:
        try:
            snap = self._inspect("baseline")
        except LookupError as exc:
            self._fail(ErrorEnvelope(error_class=ErrorClass.INVALID_SPEC, message=str(exc), operator_action="Check the table name."))
            return
        self._emit(SchemaInspected(snapshot=snap, purpose="baseline"))
        with self.s.tracer.span("GUARDRAIL", "validate_spec") as sp:
            problem = spec_problems(self.spec, snap, self.fingerprint)
            sp.set(passed=problem is None, reason=problem.message if problem else "spec matches the database")
        if problem:
            self._fail(problem)
            return
        self.s.db.bootstrap()
        plan = build_plan(self.spec, snap)
        self._emit(
            PlanCreated(plan=plan, plan_hash=plan.plan_hash(), planner="sqlgen/v1"),
            PhaseChanged(from_phase=Phase.INIT, to_phase=Phase.PLANNED, reason=plan.summary),
        )
        self.s.faults.after_plan(self.spec)

    def _planned(self) -> None:
        snap = self._inspect("drift_check")
        plan = self.state.plan
        if snap.schema_hash == plan.baseline_schema_hash:
            self._emit(SchemaInspected(snapshot=snap, purpose="drift_check"))
            self._goto(Phase.EXPAND_APPLY, "schema unchanged since plan")
            return
        conflict = column_conflict(self.spec, snap)
        self._emit(SchemaInspected(snapshot=snap, purpose="drift_check"))
        options = policy.drift_options(conflict is not None)
        if self.state.replans >= MAX_REPLANS:
            options = policy.drift_options(True)
        decision = self._decide(options) if not policy.is_forced(options) else policy.forced_decision(options[0], "schema conflict")
        if decision.kind == ActionKind.REPLAN:
            new_plan = build_plan(self.spec, snap)
            self._emit(
                SchemaInspected(snapshot=snap, purpose="baseline"),
                PlanCreated(plan=new_plan, plan_hash=new_plan.plan_hash(), planner="sqlgen/v1"),
                PhaseChanged(from_phase=Phase.PLANNED, to_phase=Phase.PLANNED, reason="replanned on the new schema baseline"),
            )
            return
        self._fail(
            conflict
            or ErrorEnvelope(
                error_class=ErrorClass.STALE_SCHEMA,
                message="schema changed after planning and the run will not apply a stale plan",
                operator_action="Review the schema change, then start a new run.",
            )
        )

    def _apply_artifacts(self, artifacts: tuple[SqlArtifact, ...], phase_tag: str) -> bool:
        """Apply SQL artifacts in order, with bounded retries. False means stop."""
        for artifact in artifacts:
            key = step_key(self.fingerprint, self.spec.migration_id, phase_tag, artifact)
            attempt = 0
            while True:
                attempt += 1
                if self._over_budget():
                    return False
                with self.s.tracer.span(
                    "TOOL", f"apply_{phase_tag}", step=artifact.name, idempotency_key=key, attempt=attempt, tx_mode=artifact.tx_mode
                ) as sp:
                    res = self.s.db.apply_artifact(self._call(f"apply_{phase_tag}", key, attempt, artifact.tx_mode), self.spec, artifact)
                    sp.set(ok=res.ok, reconciled=res.reconciled, sql_hash=artifact.sql_hash[:16], duration_ms=res.duration_ms,
                           error_class=res.error.error_class.value if res.error else "", sqlstate=res.error.sqlstate if res.error else "")
                self._emit(
                    StepRecorded(tool=f"apply_{phase_tag}:{artifact.name}", idempotency_key=key, attempt=attempt, ok=res.ok,
                                 sql_hash=artifact.sql_hash, duration_ms=res.duration_ms, lock_wait_ms=res.lock_wait_ms,
                                 reconciled=res.reconciled, error=res.error),
                    self._cost_event(res),
                )
                if res.ok:
                    break
                options = policy.step_recovery_options(self.state, res.error, attempt)
                decision = self._decide(options) if not policy.is_forced(options) else policy.forced_decision(options[0], res.error.message)
                if decision.kind == ActionKind.RETRY_STEP:
                    self.s.sleep((decision.backoff_ms or 0) / 1000)
                    continue
                if decision.kind == ActionKind.PAUSE:
                    self._pause("lock_budget", f"{artifact.name}: {res.error.message}")
                elif decision.kind == ActionKind.ABORT:
                    self._abort(f"{artifact.name} failed: {res.error.message}")
                else:
                    self._on_step_failure(res.error, artifact)
                return False
        return True

    def _on_step_failure(self, error: ErrorEnvelope, artifact: SqlArtifact) -> None:
        if self.state.phase == Phase.CONTRACT_APPLY:
            self._goto(Phase.MANUAL_INTERVENTION, f"contract step {artifact.name} failed: {error.message}")
            return
        self._fail(error.model_copy(update={"operator_action": error.operator_action or f"Step {artifact.name} failed; nothing later ran."}))

    def _expand_apply(self) -> None:
        if self._apply_artifacts(self.state.plan.expand, "expand"):
            self._goto(Phase.EXPAND_VERIFY, "additive steps applied")

    def _expand_verify(self) -> None:
        snap = self._inspect("post_expand")
        with self.s.tracer.span("EVALUATOR", "verify_expand") as sp:
            report = verifier.expand_report(self.spec, snap.schema_hash, self.s.db.expand_metrics(self.spec))
            sp.set(passed=report.passed, proof_hash=report.proof_hash[:16], violations=len(report.violations))
        self._emit(SchemaInspected(snapshot=snap, purpose="post_expand"), VerificationRecorded(report=report))
        if report.passed:
            self._goto(Phase.BACKFILL_PLAN, "expand verified")
        else:
            self._fail(ErrorEnvelope(error_class=ErrorClass.PROOF_FAILURE, message="expand did not produce the planned schema",
                                     detail={"violations": [v.model_dump() for v in report.violations]},
                                     operator_action="Inspect the expand objects; nothing was backfilled."))

    def _backfill_plan(self) -> None:
        plan = self.state.plan
        self._goto(
            Phase.BACKFILL_CHUNK_LOOP,
            f"backfill keys {plan.key_lower}-{plan.key_upper}",
            BackfillPlanned(lower=plan.key_lower, upper=plan.key_upper, batch_size=plan.initial_batch),
        )

    def _backfill_loop(self) -> None:
        st = self.state
        if st.backfill_done:
            snap = self._inspect("drift_check")
            if snap.max_key is not None and snap.max_key > st.key_upper:
                new_rows = snap.max_key - st.key_upper
                if new_rows > self.spec.chunk_policy.catch_up_row_limit:
                    self._pause("drift", f"{new_rows} keys arrived beyond the plan; above the catch-up limit")
                    return
                with self.s.tracer.span("GUARDRAIL", "drift_check") as sp:
                    sp.set(passed=False, reason=f"{new_rows} new keys beyond planned upper {st.key_upper}; running catch-up")
                self._emit(CatchUpPlanned(previous_upper=st.key_upper, new_upper=snap.max_key, new_rows=new_rows))
                return
            self._goto(Phase.BACKFILL_VERIFY, "every planned key range committed")
            return
        if self._over_budget():
            return

        if st.last_error is not None:
            options = policy.chunk_recovery_options(st)
            decision = self._decide(options) if not policy.is_forced(options) else policy.forced_decision(options[0], st.last_error.message)
            if decision.kind == ActionKind.PAUSE:
                self._pause("lock_budget", f"chunk at {st.cursor} failed {st.retry_count} times: {st.last_error.message}")
                return
            if decision.kind == ActionKind.ABORT:
                self._abort(f"operator policy chose abort after: {st.last_error.message}")
                return
            if decision.kind == ActionKind.FAIL:
                self._fail(st.last_error)
                return
            self.s.sleep((decision.backoff_ms or 0) / 1000)
        else:
            options = policy.chunk_progress_options(st)
            if not policy.is_forced(options):
                self._decide(options)

        st = self.state
        lower = st.cursor
        upper = min(lower + st.batch_size - 1, st.key_upper)
        index = len(st.chunks)
        attempt = st.retry_count + 1
        self.s.faults.before_chunk(self.spec, lower, upper, index)
        key = chunk_key(self.fingerprint, self.spec.migration_id, lower, upper, st.plan.backfill_template_hash)
        with self.s.tracer.span("TOOL", "run_backfill_chunk", lower=lower, upper=upper, batch_size=st.batch_size,
                                attempt=attempt, idempotency_key=key) as sp:
            res = self.s.db.run_chunk(self._call("run_backfill_chunk", key, attempt, lower=lower, upper=upper), self.spec, lower, upper)
            sp.set(ok=res.ok, rows=res.rows, reconciled=res.reconciled, duration_ms=res.duration_ms, lock_wait_ms=res.lock_wait_ms,
                   error_class=res.error.error_class.value if res.error else "", sqlstate=res.error.sqlstate if res.error else "")
        if res.ok:
            chunk = ChunkRecord(lower=lower, upper=upper, rows=res.rows or 0, checksum=res.checksum or "", idempotency_key=key,
                                attempts=attempt, duration_ms=res.duration_ms, reconciled=res.reconciled)
            self.s.faults.after_chunk_commit(self.spec, chunk, index)
            self._emit(
                StepRecorded(tool="run_backfill_chunk", idempotency_key=key, attempt=attempt, ok=True, rows=res.rows,
                             duration_ms=res.duration_ms, reconciled=res.reconciled),
                ChunkCommitted(chunk=chunk),
                self._cost_event(res, rows=res.rows or 0),
            )
            return
        error = self._enrich_error(res.error)
        self._emit(
            StepRecorded(tool="run_backfill_chunk", idempotency_key=key, attempt=attempt, ok=False, duration_ms=res.duration_ms,
                         lock_wait_ms=res.lock_wait_ms, error=error),
            ChunkFailed(lower=lower, upper=upper, batch_size=st.batch_size, attempt=attempt, error=error, lock_wait_ms=res.lock_wait_ms),
            self._cost_event(res),
        )

    def _enrich_error(self, error: ErrorEnvelope) -> ErrorEnvelope:
        """On lock pressure, record who holds the lock while they still hold it."""
        if error.error_class != ErrorClass.LOCK_PRESSURE:
            return error
        with self.s.tracer.span("TOOL", "inspect_locks", table=self.spec.target_table) as sp:
            blockers = self.s.db.inspect_locks(self.spec)
            sp.set(blockers=len(blockers), pids=",".join(str(b.pid) for b in blockers),
                   oldest_xact_ms=max((b.xact_age_ms or 0 for b in blockers), default=0))
        return error.model_copy(update={"blockers": blockers})

    def _backfill_verify(self) -> None:
        self.s.faults.before_backfill_verify(self.spec)
        st = self.state
        snap = self._inspect("drift_check")
        with self.s.tracer.span("EVALUATOR", "verify_backfill") as sp:
            metrics = self.s.db.backfill_metrics(self.spec)
            if int(metrics["max_key"]) > st.key_upper:
                sp.set(passed=False, reason="rows arrived after the last chunk")
                self._goto(
                    Phase.BACKFILL_CHUNK_LOOP,
                    "new rows arrived during verification",
                    CatchUpPlanned(previous_upper=st.key_upper, new_upper=int(metrics["max_key"]),
                                   new_rows=int(metrics["max_key"]) - st.key_upper),
                )
                return
            report = verifier.backfill_report(self.spec, snap.schema_hash, metrics, st.key_upper, st.cursor)
            sp.set(passed=report.passed, proof_hash=report.proof_hash[:16], mismatches=metrics["mismatches"],
                   null_targets=metrics["null_targets"], parity=report.metrics["parity"])
        self._emit(VerificationRecorded(report=report))
        if report.passed:
            self._goto(Phase.CONTRACT_GUARD, "parity proven")
            return
        self._fail(
            ErrorEnvelope(
                error_class=ErrorClass.PROOF_FAILURE,
                message="; ".join(f"{v.invariant}: {v.observed} (expected {v.expected})" for v in report.violations),
                detail={"proof_hash": report.proof_hash},
                operator_action="Contract is blocked. Find the writer that bypasses the dual-write trigger, fix the rows, then start a new run.",
            )
        )

    def _contract_guard(self) -> None:
        st = self.state
        snap = self._inspect("drift_check")
        with self.s.tracer.span("GUARDRAIL", "contract_guard") as sp:
            metrics = self.s.db.backfill_metrics(self.spec)
            report = verifier.backfill_report(self.spec, snap.schema_hash, metrics, st.key_upper, st.cursor, scope="contract_guard")
            deps = self.s.db.dependency_scan(self.spec)
            ok = report.passed and not deps
            sp.set(passed=ok, reason="fresh proof and no dependents" if ok else f"violations={len(report.violations)} deps={len(deps)}",
                   proof_hash=report.proof_hash[:16], dependencies=len(deps))
        self._emit(VerificationRecorded(report=report))
        if not report.passed:
            self._fail(ErrorEnvelope(error_class=ErrorClass.PROOF_FAILURE, message="fresh proof failed at the contract guard",
                                     operator_action="Data changed after verification. Start a new run."))
            return
        if deps:
            self._pause("operator", "contract blocked by dependents: " + "; ".join(deps))
            return
        statements = tuple(s for a in st.plan.contract for s in a.statements)
        sql_hash = sha256_hex("\n".join(statements))
        approval_id = short_hash([st.run_id, self.fingerprint, sql_hash, report.proof_hash])[:12]
        rollback = tuple(s for a in st.plan.rollback_expand for s in a.statements)
        self._emit(
            ContractPrepared(sql_hash=sql_hash, statements=statements, rollback_statements=rollback, dependencies=deps,
                             proof_hash=report.proof_hash, approval_id=approval_id),
            PhaseChanged(from_phase=Phase.CONTRACT_GUARD, to_phase=Phase.CONTRACT_READY, reason="waiting for owner approval"),
        )
        if not self.spec.approval_policy.require_owner_approval:
            self._emit(ApprovalGranted(approval_id=approval_id, approver="policy:auto", sql_hash=sql_hash))

    def _contract_ready(self) -> None:
        st = self.state
        snap = self._inspect("drift_check")
        guard = st.verifications.get("contract_guard")
        if guard is None or snap.schema_hash != guard.schema_hash:
            self._fail(ErrorEnvelope(error_class=ErrorClass.STALE_SCHEMA, message="schema changed after approval was requested",
                                     operator_action="The approval no longer matches the database. Start a new run."))
            return
        metrics = self.s.db.backfill_metrics(self.spec)
        fresh = verifier.backfill_report(self.spec, snap.schema_hash, metrics, st.key_upper, st.cursor, scope="contract_guard")
        if not fresh.passed:
            self._fail(ErrorEnvelope(error_class=ErrorClass.PROOF_FAILURE, message="proof no longer holds at apply time",
                                     operator_action="Data changed after approval. Start a new run."))
            return
        self._goto(Phase.CONTRACT_APPLY, f"approved by {st.approved_by}")

    def _contract_apply(self) -> None:
        if self._apply_artifacts(self.state.plan.contract, "contract"):
            self._goto(Phase.CONTRACT_VERIFY, "contract statements applied")

    def _contract_verify(self) -> None:
        snap = self._inspect("post_contract")
        expected = int(self.state.verifications["contract_guard"].metrics["rows_total"])
        with self.s.tracer.span("EVALUATOR", "verify_contract") as sp:
            report = verifier.contract_report(self.spec, snap.schema_hash, self.s.db.contract_metrics(self.spec), expected)
            sp.set(passed=report.passed, proof_hash=report.proof_hash[:16])
        self._emit(SchemaInspected(snapshot=snap, purpose="post_contract"), VerificationRecorded(report=report))
        if report.passed:
            self._emit(RunCompleted(proof_hash=report.proof_hash))
        else:
            self._goto(Phase.MANUAL_INTERVENTION, "contract verification failed after irreversible steps")

    def _abort(self, reason: str) -> None:
        st = self.state
        if st.terminal:
            return
        if st.phase not in (Phase.EXPAND_CLEANUP,):
            if st.phase in (Phase.CONTRACT_APPLY, Phase.CONTRACT_VERIFY):
                raise ValueError("cannot abort after contract started; use manual intervention")
            self._goto(Phase.EXPAND_CLEANUP, reason)
        if st.plan is not None:
            for artifact in st.plan.rollback_expand:
                key = short_hash(["rollback", st.run_id, artifact.name, self.state.revision])
                with self.s.tracer.span("TOOL", "rollback_expand", step=artifact.name) as sp:
                    res = self.s.db.apply_artifact(self._call("rollback_expand", key, 1, artifact.tx_mode), self.spec, artifact)
                    sp.set(ok=res.ok)
                self._emit(StepRecorded(tool=f"rollback_expand:{artifact.name}", idempotency_key=key, attempt=1, ok=res.ok,
                                        sql_hash=artifact.sql_hash, duration_ms=res.duration_ms, error=res.error))
        self._emit(RunAborted(reason=reason))
