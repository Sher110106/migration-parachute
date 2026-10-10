"""Seeded failures for the replay suite and demo. Test and demo use only.

Each fault acts through its own database connection, the way real traffic
would, so the agent sees ordinary Postgres behaviour rather than a mock.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Protocol

import psycopg

from .domain import ChunkRecord, MigrationSpec
from .sqlgen import Names, q

FAULT_KINDS = (
    "lock_timeout",
    "crash_after_commit",
    "parity_mismatch",
    "schema_drift",
    "schema_conflict",
    "concurrent_inserts",
)


class SimulatedCrash(RuntimeError):
    """The worker process dies after the database committed a chunk."""


class FaultHooks(Protocol):
    def after_plan(self, spec: MigrationSpec) -> None: ...
    def before_chunk(self, spec: MigrationSpec, lower: int, upper: int, index: int) -> None: ...
    def after_chunk_commit(self, spec: MigrationSpec, chunk: ChunkRecord, index: int) -> None: ...
    def before_backfill_verify(self, spec: MigrationSpec) -> None: ...
    def close(self) -> None: ...


class NoFaults:
    def after_plan(self, spec):
        pass

    def before_chunk(self, spec, lower, upper, index):
        pass

    def after_chunk_commit(self, spec, chunk, index):
        pass

    def before_backfill_verify(self, spec):
        pass

    def close(self):
        pass


class RowLockHolder(threading.Thread):
    """Another session holds row locks on a key range, like a slow checkout job."""

    def __init__(self, dsn: str, table: str, pk: str, lower: int, upper: int, hold_ms: int):
        super().__init__(daemon=True, name="fixture-row-lock")
        self.dsn, self.table, self.pk = dsn, table, pk
        self.lower, self.upper, self.hold_ms = lower, upper, hold_ms
        self.ready = threading.Event()
        self.released = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            with psycopg.connect(self.dsn, autocommit=True, application_name="fixture-checkout-job") as conn:
                conn.execute("BEGIN")
                conn.execute(
                    f"SELECT {q(self.pk)} FROM {q(self.table)} WHERE {q(self.pk)} BETWEEN %s AND %s FOR UPDATE",
                    (self.lower, self.upper),
                )
                self.ready.set()
                time.sleep(self.hold_ms / 1000)
                conn.execute("ROLLBACK")
        except BaseException as exc:  # surfaced to the test through .error
            self.error = exc
            self.ready.set()
        finally:
            self.released.set()


@dataclass
class FaultPlan:
    dsn: str
    kinds: frozenset[str] = frozenset()
    lock_range: tuple[int, int] = (4_001, 4_100)
    lock_hold_ms: int = 3_000
    crash_on_chunk: int = 3
    corrupt_key: int = 777
    concurrent_insert_rows: int = 240
    insert_on_chunk: int = 2
    fired: set[str] = field(default_factory=set)
    holders: list[RowLockHolder] = field(default_factory=list)

    def __post_init__(self) -> None:
        unknown = set(self.kinds) - set(FAULT_KINDS)
        if unknown:
            raise ValueError(f"unknown fault kinds: {sorted(unknown)}")

    def _once(self, kind: str) -> bool:
        if kind in self.kinds and kind not in self.fired:
            self.fired.add(kind)
            return True
        return False

    def _exec(self, sql: str, params: tuple = (), replica: bool = False) -> None:
        with psycopg.connect(self.dsn, autocommit=True, application_name="fixture-app") as conn:
            if replica:
                conn.execute("SET session_replication_role = replica")
            conn.execute(sql, params)

    def after_plan(self, spec: MigrationSpec) -> None:
        n = Names.of(spec)
        if self._once("schema_drift"):
            # A teammate ships an unrelated column between plan and expand.
            self._exec(f"ALTER TABLE {q(n.table)} ADD COLUMN IF NOT EXISTS loyalty_tier text")
        if self._once("schema_conflict"):
            # Someone created the target column with the wrong type.
            self._exec(f"ALTER TABLE {q(n.table)} ADD COLUMN IF NOT EXISTS {q(n.new)} text")

    def before_chunk(self, spec: MigrationSpec, lower: int, upper: int, index: int) -> None:
        n = Names.of(spec)
        lo, hi = self.lock_range
        if "lock_timeout" in self.kinds and "lock_timeout" not in self.fired and lower <= hi and upper >= lo:
            self.fired.add("lock_timeout")
            holder = RowLockHolder(self.dsn, n.table, n.pk, lo, hi, self.lock_hold_ms)
            holder.start()
            holder.ready.wait(5)
            if holder.error:
                raise holder.error
            self.holders.append(holder)
        if index == self.insert_on_chunk and self._once("concurrent_inserts"):
            # New orders arrive mid-backfill. Half go through the dual-write
            # trigger; half arrive through a bulk load that skips triggers.
            half = self.concurrent_insert_rows // 2
            insert = (
                f"INSERT INTO {q(n.table)} ({q(n.old)}, amount_cents, status, created_at) "
                "SELECT 'customer' || ((g %% 2000) + 1) || '@fixture.test', 500, 'paid', "
                "timestamptz '2026-10-10 12:30+05:30' + g * interval '1 second' FROM generate_series(1, %s) g"
            )
            self._exec(insert, (half,))
            self._exec(insert, (self.concurrent_insert_rows - half,), replica=True)

    def after_chunk_commit(self, spec: MigrationSpec, chunk: ChunkRecord, index: int) -> None:
        if index == self.crash_on_chunk and self._once("crash_after_commit"):
            raise SimulatedCrash(f"worker died after committing chunk {chunk.lower}-{chunk.upper}")

    def before_backfill_verify(self, spec: MigrationSpec) -> None:
        n = Names.of(spec)
        if self._once("parity_mismatch"):
            # A writer that bypasses the trigger stores the wrong customer id.
            self._exec(
                f"UPDATE {q(n.table)} SET {q(n.new)} = {q(n.new)} + 1 WHERE {q(n.pk)} = %s",
                (self.corrupt_key,),
            )

    def close(self) -> None:
        for holder in self.holders:
            holder.released.wait(10)
