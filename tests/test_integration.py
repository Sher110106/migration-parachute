"""End-to-end runs against a real local Postgres, on a separate database."""

import json
import os
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest

from parachute.domain import ActionKind, Phase, RunStatus
from parachute.faults import FaultPlan, SimulatedCrash
from parachute.fixture import demo_spec, reset_database, seed
from parachute.orchestrator import Orchestrator, Services
from parachute.pg import PostgresAdapter, RunLockHeld
from parachute.planners import AdaptivePlanner, NaiveRetryPlanner
from parachute.replay import SCENARIOS, run_scenario
from parachute.store import ControlStore

DSN = os.environ.get("PARACHUTE_TEST_DSN", "postgresql:///parachute_test")


def _pg_available() -> bool:
    try:
        with psycopg.connect("postgresql:///postgres", connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="local Postgres not running")


@pytest.fixture()
def env(tmp_path):
    reset_database(DSN)
    seed(DSN)
    db = PostgresAdapter(DSN)
    store = ControlStore(tmp_path / "control.sqlite")
    return SimpleNamespace(db=db, store=store, spec=demo_spec(db.fingerprint()), tmp=tmp_path)


def services(env, planner=None, faults=None, **kw):
    return Services(store=env.store, db=env.db, planner=planner or AdaptivePlanner(),
                    faults=faults or FaultPlan(dsn=DSN), sleep=lambda s: None if s < 0.05 else __import__("time").sleep(s), **kw)


def ledger_rows(dsn=DSN) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT count(*) FROM parachute.chunk_ledger").fetchone()[0]


def test_clean_run_stops_at_gate_then_completes_only_after_approval(env):
    orch = Orchestrator(services(env))
    st = orch.start_run(env.spec, run_id="clean")
    assert st.phase == Phase.CONTRACT_READY and st.status == RunStatus.AWAITING_APPROVAL
    assert st.verifications["backfill"].passed
    with psycopg.connect(DSN) as conn:  # nothing destructive ran
        assert conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='orders' "
                            "AND column_name='customer_email'").fetchone()[0] == 1
    with pytest.raises(ValueError):
        orch.approve_contract("clean", "wrong-id", approver="t")
    st = orch.approve_contract("clean", st.approval_id, approver="owner")
    assert st.phase == Phase.COMPLETE and st.final_proof_hash
    assert env.store.verify_chain("clean")
    assert env.store.load("clean") == env.store.materialized("clean")


def test_lock_timeout_recovers_with_adaptive_and_pauses_with_naive(env, tmp_path):
    faults = FaultPlan(dsn=DSN, kinds=frozenset({"lock_timeout"}))
    st = Orchestrator(services(env, faults=faults)).start_run(env.spec, run_id="adaptive")
    faults.close()
    assert st.phase == Phase.CONTRACT_READY and st.total_retries >= 1
    assert any(c.attempts > 1 for c in st.chunks)
    failed = [e.event for e in env.store.events("adaptive") if e.event.type == "chunk_failed"]
    assert failed and failed[0].error.sqlstate == "55P03" and failed[0].error.blockers

    reset_database(DSN)
    seed(DSN)
    spec = demo_spec(env.db.fingerprint())
    faults = FaultPlan(dsn=DSN, kinds=frozenset({"lock_timeout"}))
    st = Orchestrator(services(env, planner=NaiveRetryPlanner(), faults=faults)).start_run(spec, run_id="naive")
    faults.close()
    assert st.display_state == "PAUSED_LOCK_BUDGET"
    assert "backfill" not in st.verifications  # never claims success


def test_crash_after_commit_resumes_without_duplicate_work(env):
    faults = FaultPlan(dsn=DSN, kinds=frozenset({"crash_after_commit"}))
    with pytest.raises(SimulatedCrash):
        Orchestrator(services(env, faults=faults), owner="a").start_run(env.spec, run_id="crash")
    before = env.store.load("crash")
    committed_in_db = ledger_rows()
    assert committed_in_db == len(before.chunks) + 1  # the database is one chunk ahead
    st = Orchestrator(services(env, faults=faults), owner="b").resume_run("crash")
    assert st.phase == Phase.CONTRACT_READY
    assert sum(1 for c in st.chunks if c.reconciled) == 1
    assert ledger_rows() == len(st.chunks)  # no chunk executed twice
    assert st.fence_token > before.fence_token


def test_parity_corruption_blocks_contract(env):
    faults = FaultPlan(dsn=DSN, kinds=frozenset({"parity_mismatch"}))
    st = Orchestrator(services(env, faults=faults)).start_run(env.spec, run_id="parity")
    assert st.phase == Phase.FAILED and st.last_error.error_class.value == "PROOF_FAILURE"
    assert st.contract_sql_hash is None and st.approval_id is None


def test_invalid_spec_has_no_side_effects(env):
    spec = demo_spec(env.db.fingerprint(), primary_key="amount_cents")
    st = Orchestrator(services(env)).start_run(spec, run_id="bad")
    assert st.phase == Phase.FAILED
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT to_regnamespace('parachute')").fetchone()[0] is None


def test_budget_pause_then_resume_with_higher_cap(env):
    spec = demo_spec(env.db.fingerprint(), cost_cap_usd=Decimal("0.0000030"))
    orch = Orchestrator(services(env))
    st = orch.start_run(spec, run_id="budget")
    assert st.display_state == "PAUSED_BUDGET"
    st = orch.resume_run("budget", new_cost_cap_usd=Decimal("1"))
    assert st.phase == Phase.CONTRACT_READY


def test_second_worker_cannot_drive_a_held_run(env):
    env.store.create_run(__import__("parachute.domain", fromlist=["RunStarted"]).RunStarted(
        run_id="held", spec=env.spec, spec_hash=env.spec.spec_hash()))
    with env.db.run_lock("held"):
        with pytest.raises(RunLockHeld):
            Orchestrator(services(env)).resume_run("held")


def test_repeated_run_reconciles_and_proof_is_stable(env):
    a = Orchestrator(services(env)).start_run(env.spec, run_id="first")
    b = Orchestrator(services(env)).start_run(env.spec, run_id="second")
    assert b.phase == Phase.CONTRACT_READY
    assert all(c.reconciled for c in b.chunks)
    assert b.rows_backfilled == a.rows_backfilled  # ledger rows, not new writes
    assert b.verifications["backfill"].proof_hash == a.verifications["backfill"].proof_hash


def test_rejected_planner_answer_falls_back_and_is_recorded(env):
    class Reckless:
        name = "reckless"

        def decide(self, obs):
            from parachute.domain import Decision

            return Decision(kind=ActionKind.RETRY_CHUNK, batch_size=999_999, backoff_ms=0, summary="all at once", planner=self.name)

    faults = FaultPlan(dsn=DSN, kinds=frozenset({"lock_timeout"}))
    st = Orchestrator(services(env, planner=Reckless(), faults=faults)).start_run(env.spec, run_id="reckless")
    faults.close()
    assert st.rejected_decisions >= 1 and st.phase == Phase.CONTRACT_READY
    rejected = [e.event for e in env.store.events("reckless") if e.event.type == "decision_made" and not e.event.accepted]
    assert "batch_size 999999 outside" in rejected[0].rejection


@pytest.mark.parametrize("sid", ["schema_drift", "schema_conflict", "concurrent_inserts", "view_dependency"])
def test_replay_scenarios(sid, tmp_path):
    res = run_scenario(SCENARIOS[sid], AdaptivePlanner, dsn=DSN, var_dir=tmp_path)
    assert res["passed"], json.dumps(res, indent=1, default=str)
    assert not res["false_success"] and res["unapproved_destructive_steps"] == 0
