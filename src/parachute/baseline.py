"""The baseline the demo compares against: one UPDATE in one transaction."""

from __future__ import annotations

from .domain import ChunkCommitted, ChunkFailed, ChunkRecord, Phase, StepRecorded, short_hash
from .orchestrator import Orchestrator


class BaselineOrchestrator(Orchestrator):
    """Same plan, expand and verifier; the backfill is a single statement with no recovery."""

    mode = "baseline"

    def _backfill_loop(self) -> None:
        st = self.state
        if st.backfill_done:
            self._goto(Phase.BACKFILL_VERIFY, "single-statement backfill committed")
            return
        lower, upper = st.cursor, st.key_upper
        self.s.faults.before_chunk(self.spec, lower, upper, 0)
        key = short_hash(["naive", st.run_id, lower, upper])
        with self.s.tracer.span("TOOL", "naive_full_backfill", lower=lower, upper=upper, idempotency_key=key) as sp:
            res = self.s.db.naive_backfill(self.spec)
            sp.set(ok=res.ok, rows=res.rows, duration_ms=res.duration_ms, lock_wait_ms=res.lock_wait_ms,
                   error_class=res.error.error_class.value if res.error else "", sqlstate=res.error.sqlstate if res.error else "")
        if res.ok:
            chunk = ChunkRecord(lower=lower, upper=upper, rows=res.rows or 0, checksum="", idempotency_key=key,
                                attempts=1, duration_ms=res.duration_ms)
            self._emit(
                StepRecorded(tool="naive_full_backfill", idempotency_key=key, attempt=1, ok=True, rows=res.rows,
                             duration_ms=res.duration_ms),
                ChunkCommitted(chunk=chunk),
                self._cost_event(res, rows=res.rows or 0),
            )
            return
        error = res.error.model_copy(
            update={"operator_action": "The baseline has no recovery: the whole transaction rolled back and kept 0 rows."}
        )
        self._emit(
            StepRecorded(tool="naive_full_backfill", idempotency_key=key, attempt=1, ok=False, duration_ms=res.duration_ms,
                         lock_wait_ms=res.lock_wait_ms, error=error),
            ChunkFailed(lower=lower, upper=upper, batch_size=upper - lower + 1, attempt=1, error=error,
                        lock_wait_ms=res.lock_wait_ms),
            self._cost_event(res),
        )
        self._fail(error)
