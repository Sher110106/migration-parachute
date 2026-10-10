"""Replay suite: reset the fixture, run a scenario, check the result against
the database independently, and emit one comparable JSON record."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import psycopg

from .baseline import BaselineOrchestrator
from .domain import Phase, RunStatus, StepRecorded, ApprovalGranted
from .economics import CostModel
from .faults import FaultPlan, SimulatedCrash
from .fixture import DEFAULT_DSN, DEMO, FixtureProfile, demo_spec, reset_database, seed
from .orchestrator import Orchestrator, Services
from .pg import PostgresAdapter
from .planners import Planner
from .state import RunState
from .store import ControlStore
from .tracing import JsonlTrace, MultiTrace, NeatlogsTrace
import os


@dataclass(frozen=True)
class Scenario:
    id: str
    description: str
    expect: tuple[str, ...]  # acceptable display states
    faults: frozenset[str] = frozenset()
    mode: str = "parachute"
    spec_overrides: dict[str, Any] = field(default_factory=dict)
    profile: FixtureProfile = DEMO
    approve: bool = False


SCENARIOS: dict[str, Scenario] = {
    s.id: s
    for s in [
        Scenario("clean", "Clean additive migration; stops at the approval gate", ("CONTRACT_READY",)),
        Scenario("clean_approved", "Clean migration with owner approval; contract runs and verifies", ("COMPLETE",), approve=True),
        Scenario("baseline_lock_timeout", "Naive one-transaction backfill under the same row lock", ("FAILED",),
                 faults=frozenset({"lock_timeout"}), mode="baseline"),
        Scenario("lock_timeout", "Row lock held by another session during a chunk", ("CONTRACT_READY",),
                 faults=frozenset({"lock_timeout"})),
        Scenario("crash_after_commit", "Worker dies after the database commits a chunk; a new worker resumes",
                 ("CONTRACT_READY",), faults=frozenset({"crash_after_commit"})),
        Scenario("parity_mismatch", "A writer bypasses the trigger and stores a wrong id", ("FAILED",),
                 faults=frozenset({"parity_mismatch"})),
        Scenario("schema_drift", "Unrelated column added between plan and expand", ("CONTRACT_READY",),
                 faults=frozenset({"schema_drift"})),
        Scenario("schema_conflict", "Target column created by someone else with the wrong type", ("FAILED",),
                 faults=frozenset({"schema_conflict"})),
        Scenario("concurrent_inserts", "New orders arrive mid-backfill, some skipping triggers", ("CONTRACT_READY",),
                 faults=frozenset({"concurrent_inserts"})),
        Scenario("invalid_key", "Spec names a non-unique chunk key", ("FAILED",),
                 spec_overrides={"primary_key": "amount_cents"}),
        Scenario("budget", "Cost cap reached mid-run; pauses with checkpoint", ("PAUSED_BUDGET",),
                 spec_overrides={"cost_cap_usd": Decimal("0.0000030")}),
        Scenario("view_dependency", "A view still reads the column contract would drop", ("PAUSED_OPERATOR",),
                 profile=FixtureProfile(with_dependent_view=True)),
    ]
}

MINIMUM_SUITE = ("lock_timeout", "crash_after_commit", "parity_mismatch", "schema_drift", "budget")


def independent_check(dsn: str) -> dict[str, Any]:
    """Look at the database directly, without the agent's own reports."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        cols = {r[0] for r in conn.execute(
            "SELECT attname FROM pg_attribute WHERE attrelid = 'orders'::regclass AND attnum > 0 AND NOT attisdropped"
        ).fetchall()}
        out: dict[str, Any] = {"old_column_present": "customer_email" in cols, "new_column_present": "customer_id" in cols}
        if "customer_email" in cols and "customer_id" in cols:
            total, wrong = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE o.customer_id::text IS DISTINCT FROM c.id::text) "
                "FROM orders o LEFT JOIN customers c ON c.email = o.customer_email"
            ).fetchone()
            out.update(rows=total, wrong_or_missing=wrong)
        elif "customer_id" in cols:
            total, wrong = conn.execute(
                "SELECT count(*), count(*) FILTER (WHERE c.id IS NULL) FROM orders o LEFT JOIN customers c ON c.id = o.customer_id"
            ).fetchone()
            out.update(rows=total, wrong_or_missing=wrong)
        else:
            out.update(rows=conn.execute("SELECT count(*) FROM orders").fetchone()[0], wrong_or_missing=None)
    return out


def unapproved_destructive_steps(store: ControlStore, run_id: str) -> int:
    approved = False
    count = 0
    for env in store.events(run_id):
        e = env.event
        if isinstance(e, ApprovalGranted):
            approved = True
        if isinstance(e, StepRecorded) and e.tool.startswith("apply_contract") and e.ok and not approved:
            count += 1
    return count


def record(scenario: Scenario, state: RunState, store: ControlStore, fixture_sha: str, started: float,
           check: dict[str, Any], trace_id: str | None, planner: str, faults: FaultPlan | None = None) -> dict[str, Any]:
    proof = state.verifications.get("backfill")
    claimed_success = state.display_state in ("CONTRACT_READY", "COMPLETE")
    wrong = check.get("wrong_or_missing")
    false_success = claimed_success and (wrong is None or wrong != 0)
    metrics = proof.metrics if proof else {}
    return {
        "scenario_id": scenario.id,
        "description": scenario.description,
        "run_id": state.run_id,
        "mode": state.mode,
        "planner": planner,
        "fixture_sha": fixture_sha,
        "faults": sorted(scenario.faults),
        "lock_hold_ms": faults.lock_hold_ms if faults and "lock_timeout" in scenario.faults else None,
        "lock_budget_ms": state.spec.lock_budget_ms,
        "plan_hash": state.plan_hash,
        "terminal_state": state.display_state,
        "expected": list(scenario.expect),
        "passed": state.display_state in scenario.expect and not false_success,
        "false_success": false_success,
        "unapproved_destructive_steps": unapproved_destructive_steps(store, state.run_id),
        "rows_seen": state.baseline.row_count if state.baseline else None,
        "rows_written": state.rows_backfilled,
        "cursor": state.cursor,
        "batches": len(state.chunks),
        "reconciled_chunks": sum(1 for c in state.chunks if c.reconciled),
        "final_batch_size": state.batch_size,
        "retries": state.total_retries,
        "lock_wait_ms": state.lock_wait_ms,
        "decisions": state.decisions,
        "rejected_decisions": state.rejected_decisions,
        "replans": state.replans,
        "catch_ups": state.catch_ups,
        "source_checksum": metrics.get("source_checksum"),
        "target_checksum": metrics.get("target_checksum"),
        "null_targets": metrics.get("null_targets"),
        "mismatches": metrics.get("mismatches"),
        "proof_hash": proof.proof_hash if proof else None,
        "verifier_passed": proof.passed if proof else None,
        "last_error": state.last_error.model_dump(mode="json") if state.last_error else None,
        "tokens_in": state.tokens_in,
        "tokens_out": state.tokens_out,
        "cost_usd": str(state.cost_usd),
        "db_ms": state.db_ms,
        "independent_check": check,
        "event_count": state.revision,
        "chain_ok": store.verify_chain(state.run_id),
        "checkpoint_ref": store.latest_checkpoint_ref(state.run_id),
        "neatlogs_trace_id": trace_id,
        "wall_ms": int((time.monotonic() - started) * 1000),
    }


def make_tracer(var_dir: Path):
    sinks: list[Any] = [JsonlTrace(var_dir / "traces")]
    if os.environ.get("NEATLOGS_API_KEY"):
        sinks.append(NeatlogsTrace())
    return MultiTrace(*sinks)


def run_scenario(
    scenario: Scenario,
    planner_factory: Callable[[], Planner],
    dsn: str = DEFAULT_DSN,
    var_dir: Path = Path("var"),
    store: ControlStore | None = None,
    reset: bool = True,
    on_state: Callable[[RunState], None] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if reset:
        reset_database(dsn)
        fixture_sha = seed(dsn, scenario.profile)
    else:
        from .fixture import fixture_checksum

        fixture_sha = fixture_checksum(dsn)
    db = PostgresAdapter(dsn)
    store = store or ControlStore(var_dir / "control.sqlite")
    tracer = make_tracer(var_dir)
    faults = FaultPlan(dsn=dsn, kinds=scenario.faults)
    planner = planner_factory()
    services = Services(store=store, db=db, planner=planner, tracer=tracer, faults=faults, costs=CostModel())
    spec = demo_spec(db.fingerprint(), **scenario.spec_overrides)
    cls = BaselineOrchestrator if scenario.mode == "baseline" else Orchestrator
    run_id = run_id or f"{scenario.id}_{int(time.time() * 1000) % 10_000_000:07d}"
    try:
        state = cls(services, owner="worker-a").start_run(spec, run_id=run_id)
    except SimulatedCrash:
        # A fresh worker with no memory of the first one picks the run up.
        state = cls(services, owner="worker-b").resume_run(run_id, reason="new worker after crash")
    if scenario.approve and state.phase == Phase.CONTRACT_READY:
        state = cls(services, owner="worker-a").approve_contract(run_id, state.approval_id, approver="fixture-owner")
    faults.close()
    check = independent_check(dsn)
    out = record(scenario, state, store, fixture_sha, started, check, tracer.trace_id(run_id), planner.name, faults)
    if on_state:
        on_state(state)
    return out


def run_suite(ids: list[str], planner_factory: Callable[[], Planner], out_path: Path, dsn: str = DEFAULT_DSN,
              var_dir: Path = Path("var")) -> list[dict[str, Any]]:
    results = []
    for sid in ids:
        res = run_scenario(SCENARIOS[sid], planner_factory, dsn=dsn, var_dir=var_dir)
        results.append(res)
        print(f"{sid:24s} {res['terminal_state']:18s} {'PASS' if res['passed'] else 'FAIL'} "
              f"retries={res['retries']} lock_wait_ms={res['lock_wait_ms']} batches={res['batches']} wall_ms={res['wall_ms']}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2, default=str))
    return results
