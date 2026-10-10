"""SQLite control store: append-only events, checkpoints and projections.

Every append is one SQLite transaction guarded by the run revision (compare
and swap) and the fence token of the worker that holds the lease. A stale
worker cannot write after another worker takes the run over.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from pydantic import TypeAdapter

from .domain import (
    ChunkCommitted,
    CostRecorded,
    Event,
    EventEnvelope,
    LeaseAcquired,
    StepRecorded,
    canonical_json,
    sha256_hex,
)
from .state import RunState, apply, replay

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
GENESIS = "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS migration_runs (
    run_id TEXT PRIMARY KEY,
    spec_hash TEXT NOT NULL,
    mode TEXT NOT NULL,
    phase TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    fence_token INTEGER NOT NULL DEFAULT 0,
    head_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS migration_events (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS migration_steps (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    tool TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    sql_hash TEXT,
    rows INTEGER,
    duration_ms INTEGER NOT NULL,
    reconciled INTEGER NOT NULL,
    error_class TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS backfill_chunks (
    run_id TEXT NOT NULL,
    lower_key INTEGER NOT NULL,
    upper_key INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    reconciled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    checkpoint_ref TEXT NOT NULL,
    schema_hash TEXT,
    proof_hash TEXT,
    state_json TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS cost_ledger (
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    tokens_in INTEGER NOT NULL,
    tokens_out INTEGER NOT NULL,
    db_ms INTEGER NOT NULL,
    lock_wait_ms INTEGER NOT NULL,
    rows INTEGER NOT NULL,
    cost_usd TEXT NOT NULL,
    PRIMARY KEY (run_id, seq)
);
"""


class StoreConflict(Exception):
    """The run moved on (revision) or another worker holds the lease (fence)."""


class ControlStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._memory_conn: sqlite3.Connection | None = None
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self.path == ":memory:":
                if self._memory_conn is None:
                    self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
                yield self._memory_conn
                return
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA busy_timeout=10000")
                yield conn
            finally:
                conn.close()

    # ------------------------------------------------------------------ writes

    def create_run(self, first: Event) -> RunState:
        state = apply(None, first)
        payload = canonical_json(first)
        payload_hash = sha256_hex(GENESIS + payload)
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO migration_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        state.run_id,
                        state.spec_hash,
                        state.mode,
                        state.phase.value,
                        state.status.value,
                        state.revision,
                        0,
                        payload_hash,
                        state.model_dump_json(),
                        state.started_at,
                        state.updated_at,
                    ),
                )
                self._insert_event(conn, state.run_id, 1, first, payload, payload_hash, GENESIS)
                self._checkpoint(conn, state, 1, payload_hash)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return state

    def acquire_lease(self, run_id: str, owner: str) -> RunState:
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT revision, fence_token FROM migration_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(run_id)
                revision, fence = row
                event = LeaseAcquired(owner=owner, fence_token=fence + 1)
                state = self._append_locked(conn, run_id, [event], revision, fence, new_fence=fence + 1)
                conn.execute("COMMIT")
                return state
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def append(
        self, run_id: str, events: Sequence[Event], expected_revision: int, fence_token: int
    ) -> RunState:
        if not events:
            raise ValueError("nothing to append")
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                state = self._append_locked(conn, run_id, events, expected_revision, fence_token)
                conn.execute("COMMIT")
                return state
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _append_locked(
        self,
        conn: sqlite3.Connection,
        run_id: str,
        events: Sequence[Event],
        expected_revision: int,
        fence_token: int,
        new_fence: int | None = None,
    ) -> RunState:
        row = conn.execute(
            "SELECT revision, fence_token, head_hash, state_json FROM migration_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        revision, fence, head, state_json = row
        if revision != expected_revision:
            raise StoreConflict(f"revision is {revision}, caller expected {expected_revision}")
        if fence != fence_token:
            raise StoreConflict(f"fence token is {fence}, caller holds {fence_token}")
        state = RunState.model_validate_json(state_json)
        seq = revision
        for event in events:
            state = apply(state, event)
            seq += 1
            payload = canonical_json(event)
            payload_hash = sha256_hex(head + payload)
            self._insert_event(conn, run_id, seq, event, payload, payload_hash, head)
            head = payload_hash
        self._checkpoint(conn, state, seq, head)
        conn.execute(
            "UPDATE migration_runs SET phase=?, status=?, revision=?, fence_token=?, head_hash=?, "
            "state_json=?, updated_at=? WHERE run_id=? AND revision=?",
            (
                state.phase.value,
                state.status.value,
                state.revision,
                new_fence if new_fence is not None else fence,
                head,
                state.model_dump_json(),
                state.updated_at,
                run_id,
                expected_revision,
            ),
        )
        return state

    def _insert_event(self, conn, run_id, seq, event, payload, payload_hash, prev_hash) -> None:
        conn.execute(
            "INSERT INTO migration_events VALUES (?,?,?,?,?,?,?)",
            (run_id, seq, event.type, payload, payload_hash, prev_hash, event.at.isoformat()),
        )
        if isinstance(event, StepRecorded):
            conn.execute(
                "INSERT INTO migration_steps VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    seq,
                    event.tool,
                    event.idempotency_key,
                    event.attempt,
                    int(event.ok),
                    event.sql_hash,
                    event.rows,
                    event.duration_ms,
                    int(event.reconciled),
                    event.error.error_class.value if event.error else None,
                ),
            )
        elif isinstance(event, ChunkCommitted):
            c = event.chunk
            conn.execute(
                "INSERT INTO backfill_chunks VALUES (?,?,?,?,?,?,?,?)",
                (run_id, c.lower, c.upper, c.rows, c.checksum, c.attempts, c.idempotency_key, int(c.reconciled)),
            )
        elif isinstance(event, CostRecorded):
            conn.execute(
                "INSERT INTO cost_ledger VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    seq,
                    event.phase.value,
                    event.tokens_in,
                    event.tokens_out,
                    event.db_ms,
                    event.lock_wait_ms,
                    event.rows,
                    str(event.cost_usd),
                ),
            )

    def _checkpoint(self, conn, state: RunState, seq: int, head: str) -> None:
        proof = state.verifications.get("backfill")
        conn.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?,?,?)",
            (
                state.run_id,
                seq,
                f"ckpt:{state.run_id}:{seq}:{head[:12]}",
                state.current_schema_hash,
                proof.proof_hash if proof else None,
                state.model_dump_json(),
            ),
        )

    # ------------------------------------------------------------------- reads

    def events(self, run_id: str) -> list[EventEnvelope]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, payload_json, payload_hash, prev_hash FROM migration_events "
                "WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            EventEnvelope(
                run_id=run_id,
                seq=seq,
                event=EVENT_ADAPTER.validate_json(payload),
                payload_hash=payload_hash,
                prev_hash=prev_hash,
            )
            for seq, payload, payload_hash, prev_hash in rows
        ]

    def raw_events(self, run_id: str) -> list[tuple[int, str, str, str]]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT seq, payload_json, payload_hash, prev_hash FROM migration_events "
                "WHERE run_id=? ORDER BY seq",
                (run_id,),
            ).fetchall()

    def verify_chain(self, run_id: str) -> bool:
        head = GENESIS
        for _seq, payload, payload_hash, prev_hash in self.raw_events(run_id):
            if prev_hash != head or sha256_hex(head + payload) != payload_hash:
                return False
            head = payload_hash
        return True

    def load(self, run_id: str) -> RunState:
        """Rebuild state from the event log, not from the materialized row."""
        return replay(e.event for e in self.events(run_id))

    def materialized(self, run_id: str) -> RunState:
        with self._conn() as conn:
            row = conn.execute("SELECT state_json FROM migration_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunState.model_validate_json(row[0])

    def latest_checkpoint_ref(self, run_id: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT checkpoint_ref FROM checkpoints WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
        return row[0] if row else None

    def runs(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id, mode, phase, status, revision, created_at, updated_at FROM migration_runs "
                "ORDER BY created_at DESC"
            ).fetchall()
        keys = ("run_id", "mode", "phase", "status", "revision", "created_at", "updated_at")
        return [dict(zip(keys, r)) for r in rows]

    def chunk_rows(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT lower_key, upper_key, rows, checksum, attempts, idempotency_key, reconciled "
                "FROM backfill_chunks WHERE run_id=? ORDER BY lower_key",
                (run_id,),
            ).fetchall()
        keys = ("lower", "upper", "rows", "checksum", "attempts", "idempotency_key", "reconciled")
        return [dict(zip(keys, r)) for r in rows]

    def event_dicts(self, run_id: str, after_seq: int = 0) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seq, payload_json, payload_hash FROM migration_events WHERE run_id=? AND seq>? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
        return [{"seq": s, "hash": h, **json.loads(p)} for s, p, h in rows]
