"""SQL generated from the canonical change. The planner never writes SQL.

Identifiers are validated as plain lower-case names by the domain models and
are always double-quoted here, so generated statements are deterministic and
their hashes are stable across runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import LookupBackfillChange, MigrationSpec, SqlArtifact, sha256_hex

LEDGER_SCHEMA = "parachute"


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


@dataclass(frozen=True)
class Names:
    table: str
    pk: str
    old: str
    new: str
    new_type: str
    lookup_table: str
    lookup_match: str
    lookup_value: str
    index: str
    function: str
    trigger: str
    fk: str
    nn_check: str

    @classmethod
    def of(cls, spec: MigrationSpec) -> "Names":
        c: LookupBackfillChange = spec.change
        stem = f"{spec.target_table}_{c.new_column}"[:40]
        return cls(
            table=spec.target_table,
            pk=spec.primary_key,
            old=c.old_column,
            new=c.new_column,
            new_type=c.new_type,
            lookup_table=c.lookup_table,
            lookup_match=c.lookup_match_column,
            lookup_value=c.lookup_value_column,
            index=f"{stem}_idx",
            function=f"{spec.migration_id[:40]}_dual_write",
            trigger=f"{spec.migration_id[:40]}_dual_write",
            fk=f"{stem}_fkey",
            nn_check=f"{stem}_not_null",
        )


def bootstrap_sql() -> tuple[str, ...]:
    """Ledger tables in their own schema. Additive and outside the app's tables."""
    s = q(LEDGER_SCHEMA)
    return (
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"CREATE TABLE IF NOT EXISTS {s}.chunk_ledger ("
        " idempotency_key text PRIMARY KEY,"
        " migration_id text NOT NULL,"
        " run_id text NOT NULL,"
        " lower_key bigint NOT NULL,"
        " upper_key bigint NOT NULL,"
        " rows integer NOT NULL,"
        " checksum text NOT NULL,"
        " committed_at timestamptz NOT NULL DEFAULT now())",
        f"CREATE TABLE IF NOT EXISTS {s}.step_ledger ("
        " idempotency_key text PRIMARY KEY,"
        " migration_id text NOT NULL,"
        " run_id text NOT NULL,"
        " step text NOT NULL,"
        " sql_hash text NOT NULL,"
        " committed_at timestamptz NOT NULL DEFAULT now())",
    )


def expand_artifacts(spec: MigrationSpec) -> tuple[SqlArtifact, ...]:
    n = Names.of(spec)
    fn = f"{q(LEDGER_SCHEMA)}.{q(n.function)}"
    return (
        SqlArtifact(
            name="add_column",
            statements=(f"ALTER TABLE {q(n.table)} ADD COLUMN IF NOT EXISTS {q(n.new)} {n.new_type}",),
        ),
        SqlArtifact(
            name="dual_write_trigger",
            statements=(
                f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger LANGUAGE plpgsql AS $fn$ "
                f"BEGIN NEW.{q(n.new)} := (SELECT l.{q(n.lookup_value)} FROM {q(n.lookup_table)} l "
                f"WHERE l.{q(n.lookup_match)} = NEW.{q(n.old)}); RETURN NEW; END $fn$",
                f"CREATE OR REPLACE TRIGGER {q(n.trigger)} BEFORE INSERT OR UPDATE OF {q(n.old)} "
                f"ON {q(n.table)} FOR EACH ROW EXECUTE FUNCTION {fn}()",
            ),
        ),
        SqlArtifact(
            name="create_index",
            statements=(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {q(n.index)} ON {q(n.table)} ({q(n.new)})",),
            tx_mode="autocommit",
        ),
    )


def rollback_expand_artifacts(spec: MigrationSpec) -> tuple[SqlArtifact, ...]:
    """Remove only what expand added. The original columns are never touched."""
    n = Names.of(spec)
    return (
        SqlArtifact(
            name="drop_dual_write",
            statements=(
                f"DROP TRIGGER IF EXISTS {q(n.trigger)} ON {q(n.table)}",
                f"DROP FUNCTION IF EXISTS {q(LEDGER_SCHEMA)}.{q(n.function)}()",
            ),
        ),
        SqlArtifact(
            name="drop_index",
            statements=(f"DROP INDEX CONCURRENTLY IF EXISTS {q(n.index)}",),
            tx_mode="autocommit",
        ),
        SqlArtifact(
            name="drop_column",
            statements=(f"ALTER TABLE {q(n.table)} DROP COLUMN IF EXISTS {q(n.new)}",),
        ),
    )


def contract_artifacts(spec: MigrationSpec) -> tuple[SqlArtifact, ...]:
    n = Names.of(spec)
    t = q(n.table)
    out: list[SqlArtifact] = []
    if spec.change.add_foreign_key:
        out += [
            SqlArtifact(
                name="add_fk_not_valid",
                statements=(
                    f"ALTER TABLE {t} ADD CONSTRAINT {q(n.fk)} FOREIGN KEY ({q(n.new)}) "
                    f"REFERENCES {q(n.lookup_table)} ({q(n.lookup_value)}) NOT VALID",
                ),
            ),
            SqlArtifact(name="validate_fk", statements=(f"ALTER TABLE {t} VALIDATE CONSTRAINT {q(n.fk)}",)),
        ]
    out += [
        SqlArtifact(
            name="add_not_null_check",
            statements=(f"ALTER TABLE {t} ADD CONSTRAINT {q(n.nn_check)} CHECK ({q(n.new)} IS NOT NULL) NOT VALID",),
        ),
        SqlArtifact(name="validate_not_null_check", statements=(f"ALTER TABLE {t} VALIDATE CONSTRAINT {q(n.nn_check)}",)),
        SqlArtifact(
            name="set_not_null",
            statements=(
                f"ALTER TABLE {t} ALTER COLUMN {q(n.new)} SET NOT NULL",
                f"ALTER TABLE {t} DROP CONSTRAINT {q(n.nn_check)}",
            ),
        ),
        SqlArtifact(
            name="drop_dual_write",
            statements=(
                f"DROP TRIGGER IF EXISTS {q(n.trigger)} ON {t}",
                f"DROP FUNCTION IF EXISTS {q(LEDGER_SCHEMA)}.{q(n.function)}()",
            ),
        ),
    ]
    if spec.change.drop_old_column:
        out.append(SqlArtifact(name="drop_old_column", statements=(f"ALTER TABLE {t} DROP COLUMN {q(n.old)}",)))
    return tuple(out)


def backfill_chunk_sql(spec: MigrationSpec) -> str:
    n = Names.of(spec)
    return (
        f"UPDATE {q(n.table)} AS o SET {q(n.new)} = l.{q(n.lookup_value)} "
        f"FROM {q(n.lookup_table)} AS l "
        f"WHERE l.{q(n.lookup_match)} = o.{q(n.old)} "
        f"AND o.{q(n.pk)} BETWEEN %(lower)s AND %(upper)s "
        f"AND o.{q(n.new)} IS DISTINCT FROM l.{q(n.lookup_value)}"
    )


def chunk_checksum_sql(spec: MigrationSpec) -> str:
    n = Names.of(spec)
    return (
        f"SELECT count(*), md5(coalesce(string_agg(o.{q(n.pk)}::text || ':' || "
        f"coalesce(o.{q(n.new)}::text, '-'), ',' ORDER BY o.{q(n.pk)}), '')) "
        f"FROM {q(n.table)} o WHERE o.{q(n.pk)} BETWEEN %(lower)s AND %(upper)s"
    )


def naive_backfill_sql(spec: MigrationSpec) -> str:
    """The baseline: one statement, one transaction, the whole table."""
    n = Names.of(spec)
    return (
        f"UPDATE {q(n.table)} AS o SET {q(n.new)} = l.{q(n.lookup_value)} "
        f"FROM {q(n.lookup_table)} AS l WHERE l.{q(n.lookup_match)} = o.{q(n.old)}"
    )


def backfill_template_hash(spec: MigrationSpec) -> str:
    return sha256_hex(backfill_chunk_sql(spec) + "\n" + chunk_checksum_sql(spec))


def verify_backfill_sql(spec: MigrationSpec) -> str:
    n = Names.of(spec)
    o_old, o_new, l_val = f"o.{q(n.old)}", f"o.{q(n.new)}", f"l.{q(n.lookup_value)}"
    pk = f"o.{q(n.pk)}"
    return (
        "SELECT "
        "count(*) AS rows_total, "
        f"coalesce(max({pk}), 0) AS max_key, "
        f"count(*) FILTER (WHERE {o_old} IS NOT NULL) AS rows_with_source, "
        f"count(*) FILTER (WHERE {o_old} IS NOT NULL AND {l_val} IS NULL) AS orphan_sources, "
        f"count(*) FILTER (WHERE {l_val} IS NOT NULL AND {o_new} IS NULL) AS null_targets, "
        f"count(*) FILTER (WHERE {o_new} IS DISTINCT FROM {l_val}) AS mismatches, "
        f"md5(coalesce(string_agg({pk}::text || ':' || coalesce({l_val}::text, '-'), ',' ORDER BY {pk}), '')) "
        "AS source_checksum, "
        f"md5(coalesce(string_agg({pk}::text || ':' || coalesce({o_new}::text, '-'), ',' ORDER BY {pk}), '')) "
        "AS target_checksum "
        f"FROM {q(n.table)} o LEFT JOIN {q(n.lookup_table)} l ON l.{q(n.lookup_match)} = {o_old}"
    )
