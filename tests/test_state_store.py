from decimal import Decimal

import pytest

from parachute.domain import (
    ChunkCommitted,
    ChunkFailed,
    ChunkRecord,
    ErrorClass,
    ErrorEnvelope,
    LookupBackfillChange,
    MigrationSpec,
    Phase,
    PhaseChanged,
    RunStarted,
    RunStatus,
    BackfillPlanned,
)
from parachute.state import IllegalTransition, apply, replay
from parachute.store import ControlStore, StoreConflict


def make_spec(**overrides) -> MigrationSpec:
    base = dict(
        migration_id="orders_customer_fk",
        database_fingerprint="test",
        target_table="orders",
        primary_key="id",
        change=LookupBackfillChange(
            old_column="customer_email",
            new_column="customer_id",
            lookup_table="customers",
            lookup_match_column="email",
            lookup_value_column="id",
        ),
    )
    base.update(overrides)
    return MigrationSpec(**base)


def started(run_id="run_test") -> RunStarted:
    spec = make_spec()
    return RunStarted(run_id=run_id, spec=spec, spec_hash=spec.spec_hash())


def test_spec_rejects_non_identifier():
    with pytest.raises(ValueError):
        make_spec(target_table="orders; drop table customers")


def test_spec_hash_is_stable():
    assert make_spec().spec_hash() == make_spec().spec_hash()
    assert make_spec().spec_hash() != make_spec(lock_budget_ms=999).spec_hash()


def test_reducer_rejects_skipped_phase():
    state = apply(None, started())
    with pytest.raises(IllegalTransition):
        apply(state, PhaseChanged(from_phase=Phase.INIT, to_phase=Phase.CONTRACT_APPLY))


def test_reducer_cannot_reach_contract_ready_without_guard():
    state = apply(None, started())
    with pytest.raises(IllegalTransition):
        apply(state, PhaseChanged(from_phase=Phase.INIT, to_phase=Phase.CONTRACT_READY))


def test_chunk_must_start_at_cursor():
    state = apply(None, started())
    state = apply(state, BackfillPlanned(lower=1, upper=100, batch_size=10))
    good = ChunkRecord(lower=1, upper=10, rows=10, checksum="c", idempotency_key="k1", attempts=1, duration_ms=1)
    state = apply(state, ChunkCommitted(chunk=good))
    assert state.cursor == 11 and state.rows_backfilled == 10
    bad = good.model_copy(update={"lower": 50, "upper": 59, "idempotency_key": "k2"})
    with pytest.raises(IllegalTransition):
        apply(state, ChunkCommitted(chunk=bad))


def test_failure_then_success_resets_retry_count():
    state = apply(None, started())
    state = apply(state, BackfillPlanned(lower=1, upper=100, batch_size=10))
    err = ErrorEnvelope(error_class=ErrorClass.LOCK_PRESSURE, sqlstate="55P03", message="lock timeout", retryable=True)
    state = apply(state, ChunkFailed(lower=1, upper=10, batch_size=10, attempt=1, error=err))
    assert state.retry_count == 1 and state.last_error is not None
    rec = ChunkRecord(lower=1, upper=10, rows=10, checksum="c", idempotency_key="k1", attempts=2, duration_ms=1)
    state = apply(state, ChunkCommitted(chunk=rec))
    assert state.retry_count == 0 and state.total_retries == 1 and state.last_error is None


def test_store_replay_matches_materialized_and_chain_verifies(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite")
    store.create_run(started())
    state = store.acquire_lease("run_test", "w1")
    state = store.append(
        "run_test",
        [PhaseChanged(from_phase=Phase.INIT, to_phase=Phase.PLANNED)],
        state.revision,
        state.fence_token,
    )
    assert store.load("run_test") == store.materialized("run_test")
    assert store.verify_chain("run_test")
    assert store.latest_checkpoint_ref("run_test").startswith("ckpt:run_test:3:")


def test_store_rejects_stale_revision(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite")
    store.create_run(started())
    state = store.acquire_lease("run_test", "w1")
    with pytest.raises(StoreConflict):
        store.append("run_test", [PhaseChanged(from_phase=Phase.INIT, to_phase=Phase.PLANNED)], state.revision - 1, state.fence_token)


def test_second_lease_fences_out_first_worker(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite")
    store.create_run(started())
    first = store.acquire_lease("run_test", "w1")
    second = store.acquire_lease("run_test", "w2")
    assert second.fence_token == first.fence_token + 1
    with pytest.raises(StoreConflict):
        store.append(
            "run_test",
            [PhaseChanged(from_phase=Phase.INIT, to_phase=Phase.PLANNED)],
            second.revision,
            first.fence_token,
        )


def test_duplicate_chunk_idempotency_key_is_rejected(tmp_path):
    store = ControlStore(tmp_path / "control.sqlite")
    store.create_run(started())
    s = store.acquire_lease("run_test", "w1")
    s = store.append("run_test", [BackfillPlanned(lower=1, upper=100, batch_size=10)], s.revision, s.fence_token)
    rec = ChunkRecord(lower=1, upper=10, rows=10, checksum="c", idempotency_key="same", attempts=1, duration_ms=1)
    s = store.append("run_test", [ChunkCommitted(chunk=rec)], s.revision, s.fence_token)
    dup = rec.model_copy(update={"lower": 11, "upper": 20})
    with pytest.raises(Exception):
        store.append("run_test", [ChunkCommitted(chunk=dup)], s.revision, s.fence_token)
    assert store.load("run_test").rows_backfilled == 10


def test_tampered_event_breaks_chain(tmp_path):
    import sqlite3

    path = tmp_path / "control.sqlite"
    store = ControlStore(path)
    store.create_run(started())
    store.acquire_lease("run_test", "w1")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE migration_events SET payload_json = replace(payload_json, 'w1', 'w9') WHERE seq = 2")
    conn.commit()
    conn.close()
    assert not store.verify_chain("run_test")


def test_paused_resume_restores_status():
    from parachute.domain import RunPaused, RunResumed

    state = apply(None, started())
    state = apply(state, RunPaused(reason="budget"))
    assert state.display_state == "PAUSED_BUDGET"
    state = apply(state, RunResumed(new_cost_cap_usd=Decimal("2")))
    assert state.status == RunStatus.RUNNING and state.cost_cap_usd == Decimal("2")


def test_replay_is_deterministic():
    events = [started(), BackfillPlanned(lower=1, upper=20, batch_size=10)]
    assert replay(events) == replay(events)
