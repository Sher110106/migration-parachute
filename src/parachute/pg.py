"""Postgres adapter: typed tool calls against the target database.

Every side effect writes an idempotency ledger row in the same transaction,
so a crash between "database committed" and "control store recorded" is
reconciled by reading the ledger, never by blindly re-running SQL.
"""

from __future__ import annotations

import re
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg

from .domain import (
    Blocker,
    ColumnInfo,
    ErrorClass,
    ErrorEnvelope,
    MigrationSpec,
    SchemaSnapshot,
    SqlArtifact,
    ToolCall,
    ToolResult,
    sha256_hex,
    short_hash,
)
from .sqlgen import (
    LEDGER_SCHEMA,
    Names,
    bootstrap_sql,
    backfill_chunk_sql,
    chunk_checksum_sql,
    naive_backfill_sql,
    q,
    verify_backfill_sql,
)

TRANSIENT_STATES = {"40001", "40P01"}
LOCK_STATES = {"55P03", "57014"}
SCHEMA_STATES = {"42601", "42703", "42P01", "42P07", "42701", "42804", "42883"}
PERMISSION_STATES = {"42501"}
PROOF_STATES = {"23502", "23503", "23505", "23514"}

_VALUE_RE = re.compile(r"\((.*?)\)=\((.*?)\)")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def redact(message: str) -> str:
    """Drop row values that Postgres puts in error details."""
    return _VALUE_RE.sub("(…)=(…)", message.strip().splitlines()[0] if message else "")[:240]


def classify(exc: BaseException) -> ErrorEnvelope:
    sqlstate = getattr(exc, "sqlstate", None)
    message = redact(str(exc))
    if sqlstate in TRANSIENT_STATES:
        cls = ErrorClass.TRANSIENT
    elif sqlstate in LOCK_STATES:
        cls = ErrorClass.LOCK_PRESSURE
    elif sqlstate in SCHEMA_STATES:
        cls = ErrorClass.FATAL_SCHEMA
    elif sqlstate in PERMISSION_STATES:
        cls = ErrorClass.PERMISSION_POLICY
    elif sqlstate in PROOF_STATES:
        cls = ErrorClass.PROOF_FAILURE
    elif sqlstate is None and isinstance(exc, psycopg.OperationalError):
        cls = ErrorClass.TRANSIENT
    elif sqlstate and sqlstate.startswith("08"):
        cls = ErrorClass.TRANSIENT
    else:
        cls = ErrorClass.UNKNOWN
    return ErrorEnvelope(
        error_class=cls,
        sqlstate=sqlstate,
        message=message,
        retryable=cls in (ErrorClass.TRANSIENT, ErrorClass.LOCK_PRESSURE),
    )


class RunLockHeld(Exception):
    pass


class PostgresAdapter:
    def __init__(self, dsn: str, application_name: str = "parachute"):
        self.dsn = dsn
        self.application_name = application_name

    # ------------------------------------------------------------- plumbing

    def connect(self, app_suffix: str = "") -> psycopg.Connection:
        return psycopg.connect(
            self.dsn,
            autocommit=True,
            application_name=f"{self.application_name}{app_suffix}",
            connect_timeout=5,
        )

    def fingerprint(self) -> str:
        with self.connect() as conn:
            try:
                sysid = conn.execute("SELECT system_identifier FROM pg_control_system()").fetchone()[0]
            except psycopg.Error:
                sysid = "unknown"
            db = conn.execute("SELECT current_database()").fetchone()[0]
        return f"pg:{sysid}:{db}"

    def bootstrap(self) -> None:
        with self.connect() as conn, conn.transaction():
            for stmt in bootstrap_sql():
                conn.execute(stmt)

    @contextmanager
    def run_lock(self, run_id: str) -> Iterator[None]:
        """Session advisory lock: one worker per run against this database."""
        key = zlib.crc32(run_id.encode()) & 0x7FFFFFFF
        conn = self.connect("-lease")
        try:
            got = conn.execute("SELECT pg_try_advisory_lock(%s, %s)", (4242, key)).fetchone()[0]
            if not got:
                raise RunLockHeld(run_id)
            yield
        finally:
            conn.close()

    # ------------------------------------------------------------ inspection

    def inspect(self, spec: MigrationSpec) -> SchemaSnapshot:
        n = Names.of(spec)
        with self.connect() as conn:
            rel = conn.execute("SELECT to_regclass(%s)", (q(n.table),)).fetchone()[0]
            if rel is None:
                raise LookupError(f"table {n.table} does not exist")
            cols = conn.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attname",
                (q(n.table),),
            ).fetchall()
            indexes = conn.execute(
                "SELECT pg_get_indexdef(indexrelid) || CASE WHEN indisvalid THEN '' ELSE ' INVALID' END "
                "FROM pg_index WHERE indrelid = %s::regclass ORDER BY 1",
                (q(n.table),),
            ).fetchall()
            constraints = conn.execute(
                "SELECT conname || ' ' || pg_get_constraintdef(oid) || "
                "CASE WHEN convalidated THEN '' ELSE ' NOT VALIDATED' END "
                "FROM pg_constraint WHERE conrelid = %s::regclass ORDER BY 1",
                (q(n.table),),
            ).fetchall()
            triggers = conn.execute(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgrelid = %s::regclass AND NOT tgisinternal ORDER BY 1",
                (q(n.table),),
            ).fetchall()
            count, lo, hi = conn.execute(
                f"SELECT count(*), min({q(n.pk)}), max({q(n.pk)}) FROM {q(n.table)}"
            ).fetchone()
            key_ok = self._unique_not_null(conn, n.table, n.pk)
            lookup_ok = self._unique_not_null(conn, n.lookup_table, n.lookup_match, allow_nullable=True)
        columns = tuple(ColumnInfo(name=c[0], data_type=c[1], not_null=c[2], default=c[3]) for c in cols)
        key_col = next((c for c in columns if c.name == n.pk), None)
        key_ok = key_ok and key_col is not None and key_col.data_type in ("integer", "bigint", "smallint")
        shape = {
            "columns": [c.model_dump() for c in columns],
            "indexes": [i[0] for i in indexes],
            "constraints": [c[0] for c in constraints],
            "triggers": [t[0] for t in triggers],
        }
        return SchemaSnapshot(
            table=n.table,
            columns=columns,
            indexes=tuple(i[0] for i in indexes),
            constraints=tuple(c[0] for c in constraints),
            triggers=tuple(t[0] for t in triggers),
            schema_hash=sha256_hex(shape),
            row_count=count,
            min_key=lo,
            max_key=hi,
            key_unique_not_null=key_ok,
            lookup_unique=lookup_ok,
        )

    @staticmethod
    def _unique_not_null(conn, table: str, column: str, allow_nullable: bool = False) -> bool:
        if conn.execute("SELECT to_regclass(%s)", (q(table),)).fetchone()[0] is None:
            return False
        row = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
            "WHERE i.indrelid = %s::regclass AND i.indisunique AND i.indisvalid "
            "AND i.indnkeyatts = 1 AND i.indpred IS NULL AND a.attname = %s "
            "AND (a.attnotnull OR %s))",
            (q(table), column, allow_nullable),
        ).fetchone()
        return bool(row[0])

    def inspect_locks(self, spec: MigrationSpec) -> tuple[Blocker, ...]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT l.pid, l.mode, l.granted, a.state, "
                "(extract(epoch FROM (now() - a.xact_start)) * 1000)::int, md5(coalesce(a.query, '')) "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.relation = %s::regclass AND l.pid <> pg_backend_pid() "
                "AND a.application_name NOT LIKE %s ORDER BY l.pid",
                (q(spec.target_table), f"{self.application_name}%"),
            ).fetchall()
        return tuple(
            Blocker(pid=r[0], lock_mode=r[1], granted=r[2], state=r[3], xact_age_ms=r[4], query_hash=r[5][:12])
            for r in rows
        )

    # ------------------------------------------------------------ side effects

    def apply_artifact(self, call: ToolCall, spec: MigrationSpec, artifact: SqlArtifact) -> ToolResult:
        started = _now()
        t0 = time.monotonic()
        try:
            with self.connect() as conn:
                if artifact.tx_mode == "transactional":
                    with conn.transaction():
                        self._timeouts(conn, spec, local=True)
                        if self._step_done(conn, call.idempotency_key):
                            return self._ok(started, artifact, reconciled=True)
                        for stmt in artifact.statements:
                            conn.execute(stmt)
                        self._record_step(conn, call, spec, artifact)
                else:
                    self._timeouts(conn, spec, local=False)
                    if self._step_done(conn, call.idempotency_key):
                        return self._ok(started, artifact, reconciled=True)
                    self._drop_invalid_index(conn, spec, artifact)
                    for stmt in artifact.statements:
                        conn.execute(stmt)
                    self._record_step(conn, call, spec, artifact)
        except psycopg.Error as exc:
            env = classify(exc)
            waited = int((time.monotonic() - t0) * 1000) if env.error_class == ErrorClass.LOCK_PRESSURE else 0
            return ToolResult(
                ok=False, sql_hash=artifact.sql_hash, started_at=started, ended_at=_now(), lock_wait_ms=waited, error=env
            )
        return self._ok(started, artifact)

    def _drop_invalid_index(self, conn, spec: MigrationSpec, artifact: SqlArtifact) -> None:
        if artifact.name != "create_index":
            return
        name = Names.of(spec).index
        row = conn.execute(
            "SELECT i.indisvalid FROM pg_index i WHERE i.indexrelid = to_regclass(%s)", (q(name),)
        ).fetchone()
        if row is not None and not row[0]:
            conn.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {q(name)}")

    @staticmethod
    def _ok(started, artifact: SqlArtifact, reconciled: bool = False) -> ToolResult:
        return ToolResult(ok=True, sql_hash=artifact.sql_hash, started_at=started, ended_at=_now(), reconciled=reconciled)

    def _timeouts(self, conn, spec: MigrationSpec, local: bool) -> None:
        scope = "LOCAL " if local else ""
        conn.execute(f"SET {scope}lock_timeout = '{int(spec.lock_budget_ms)}ms'")
        conn.execute(f"SET {scope}statement_timeout = '{int(spec.statement_timeout_ms)}ms'")

    @staticmethod
    def _step_done(conn, key: str) -> bool:
        return (
            conn.execute(
                f"SELECT 1 FROM {q(LEDGER_SCHEMA)}.step_ledger WHERE idempotency_key = %s", (key,)
            ).fetchone()
            is not None
        )

    @staticmethod
    def _record_step(conn, call: ToolCall, spec: MigrationSpec, artifact: SqlArtifact) -> None:
        conn.execute(
            f"INSERT INTO {q(LEDGER_SCHEMA)}.step_ledger (idempotency_key, migration_id, run_id, step, sql_hash) "
            "VALUES (%s, %s, %s, %s, %s)",
            (call.idempotency_key, spec.migration_id, call.run_id, artifact.name, artifact.sql_hash),
        )

    def run_chunk(self, call: ToolCall, spec: MigrationSpec, lower: int, upper: int) -> ToolResult:
        started = _now()
        t0 = time.monotonic()
        params = {"lower": lower, "upper": upper}
        try:
            with self.connect() as conn, conn.transaction():
                self._timeouts(conn, spec, local=True)
                prior = conn.execute(
                    f"SELECT rows, checksum FROM {q(LEDGER_SCHEMA)}.chunk_ledger WHERE idempotency_key = %s",
                    (call.idempotency_key,),
                ).fetchone()
                if prior is not None:
                    return ToolResult(
                        ok=True, rows=prior[0], checksum=prior[1], started_at=started, ended_at=_now(), reconciled=True
                    )
                updated = conn.execute(backfill_chunk_sql(spec), params).rowcount
                in_range, checksum = conn.execute(chunk_checksum_sql(spec), params).fetchone()
                conn.execute(
                    f"INSERT INTO {q(LEDGER_SCHEMA)}.chunk_ledger "
                    "(idempotency_key, migration_id, run_id, lower_key, upper_key, rows, checksum) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (call.idempotency_key, spec.migration_id, call.run_id, lower, upper, updated, checksum),
                )
        except psycopg.Error as exc:
            env = classify(exc)
            waited = int((time.monotonic() - t0) * 1000) if env.error_class == ErrorClass.LOCK_PRESSURE else 0
            return ToolResult(ok=False, started_at=started, ended_at=_now(), lock_wait_ms=waited, error=env)
        return ToolResult(
            ok=True,
            rows=updated,
            checksum=checksum,
            started_at=started,
            ended_at=_now(),
            value={"rows_in_range": in_range},
        )

    def ledger_chunks(self, migration_id: str, from_key: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT idempotency_key, lower_key, upper_key, rows, checksum FROM {q(LEDGER_SCHEMA)}.chunk_ledger "
                "WHERE migration_id = %s AND lower_key >= %s ORDER BY lower_key",
                (migration_id, from_key),
            ).fetchall()
        keys = ("idempotency_key", "lower", "upper", "rows", "checksum")
        return [dict(zip(keys, r)) for r in rows]

    def naive_backfill(self, spec: MigrationSpec) -> ToolResult:
        """Baseline: the whole table in one transaction under the same lock budget."""
        started = _now()
        t0 = time.monotonic()
        try:
            with self.connect("-baseline") as conn, conn.transaction():
                self._timeouts(conn, spec, local=True)
                updated = conn.execute(naive_backfill_sql(spec)).rowcount
        except psycopg.Error as exc:
            env = classify(exc)
            waited = int((time.monotonic() - t0) * 1000) if env.error_class == ErrorClass.LOCK_PRESSURE else 0
            return ToolResult(ok=False, started_at=started, ended_at=_now(), lock_wait_ms=waited, error=env)
        return ToolResult(ok=True, rows=updated, started_at=started, ended_at=_now())

    # ------------------------------------------------------------ verification

    def expand_metrics(self, spec: MigrationSpec) -> dict[str, Any]:
        n = Names.of(spec)
        with self.connect() as conn:
            col = conn.execute(
                "SELECT format_type(atttypid, atttypmod), attnotnull FROM pg_attribute "
                "WHERE attrelid = %s::regclass AND attname = %s AND NOT attisdropped",
                (q(n.table), n.new),
            ).fetchone()
            idx = conn.execute(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)", (q(n.index),)
            ).fetchone()
            trg = conn.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s",
                (q(n.table), n.trigger),
            ).fetchone()
        return {
            "new_column_type": col[0] if col else None,
            "new_column_nullable": (not col[1]) if col else None,
            "index_valid": bool(idx and idx[0]),
            "dual_write_trigger": bool(trg and trg[0] != "D"),
        }

    def backfill_metrics(self, spec: MigrationSpec) -> dict[str, Any]:
        """One consistent snapshot of parity, nulls, orphans and drift."""
        with self.connect() as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            try:
                cur = conn.execute(verify_backfill_sql(spec))
                names = [d.name for d in cur.description]
                row = dict(zip(names, cur.fetchone()))
            finally:
                conn.execute("ROLLBACK")
        row.update(self.expand_metrics(spec))
        return row

    def dependency_scan(self, spec: MigrationSpec) -> tuple[str, ...]:
        """Objects that still reference the column contract will drop."""
        n = Names.of(spec)
        found: list[str] = []
        with self.connect() as conn:
            attnum = conn.execute(
                "SELECT attnum FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s AND NOT attisdropped",
                (q(n.table), n.old),
            ).fetchone()
            if attnum is None:
                return ()
            views = conn.execute(
                "SELECT DISTINCT v.relname FROM pg_depend d "
                "JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class "
                "WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = %s::regclass "
                "AND d.refobjsubid = %s AND v.oid <> %s::regclass ORDER BY 1",
                (q(n.table), attnum[0], q(n.table)),
            ).fetchall()
            found += [f"view {v[0]} reads {n.table}.{n.old}" for v in views]
            idx = conn.execute(
                "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE i.indrelid = %s::regclass AND %s = ANY(i.indkey::int2[]) ORDER BY 1",
                (q(n.table), attnum[0]),
            ).fetchall()
            found += [f"index {r[0]} covers {n.table}.{n.old}" for r in idx]
            funcs = conn.execute(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace s ON s.oid = p.pronamespace "
                "WHERE s.nspname NOT IN ('pg_catalog', 'information_schema', %s) "
                "AND p.prosrc ILIKE %s ORDER BY 1",
                (LEDGER_SCHEMA, f"%{n.old}%"),
            ).fetchall()
            found += [f"function {r[0]} mentions {n.old}" for r in funcs]
        return tuple(found)

    def contract_metrics(self, spec: MigrationSpec) -> dict[str, Any]:
        n = Names.of(spec)
        with self.connect() as conn:
            old = conn.execute(
                "SELECT 1 FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s AND NOT attisdropped",
                (q(n.table), n.old),
            ).fetchone()
            new = conn.execute(
                "SELECT attnotnull FROM pg_attribute WHERE attrelid = %s::regclass AND attname = %s AND NOT attisdropped",
                (q(n.table), n.new),
            ).fetchone()
            fk = conn.execute(
                "SELECT convalidated FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s",
                (q(n.table), n.fk),
            ).fetchone()
            rows, joined = conn.execute(
                f"SELECT count(*), count(l.{q(n.lookup_value)}) FROM {q(n.table)} o "
                f"LEFT JOIN {q(n.lookup_table)} l ON l.{q(n.lookup_value)} = o.{q(n.new)}"
            ).fetchone()
            trg = conn.execute(
                "SELECT 1 FROM pg_trigger WHERE tgrelid = %s::regclass AND tgname = %s", (q(n.table), n.trigger)
            ).fetchone()
        return {
            "old_column_present": old is not None,
            "new_column_not_null": bool(new and new[0]),
            "fk_validated": bool(fk and fk[0]) if spec.change.add_foreign_key else True,
            "rows_total": rows,
            "rows_joined": joined,
            "dual_write_trigger": trg is not None,
        }


def chunk_key(fingerprint: str, migration_id: str, lower: int, upper: int, template_hash: str) -> str:
    return short_hash(["backfill", fingerprint, migration_id, lower, upper, template_hash])


def step_key(fingerprint: str, migration_id: str, phase: str, artifact: SqlArtifact) -> str:
    return short_hash([phase, fingerprint, migration_id, artifact.name, artifact.sql_hash])
