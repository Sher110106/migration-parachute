"""Planners choose one bounded action from the options policy allows."""

from __future__ import annotations

import zlib
from typing import Protocol

from .domain import ActionKind, Decision, ErrorClass, Observation


class Planner(Protocol):
    name: str

    def decide(self, obs: Observation) -> Decision: ...


def _pick(obs: Observation, *kinds: ActionKind):
    for kind in kinds:
        for option in obs.options:
            if option.kind == kind:
                return option
    return obs.options[0]


class NaiveRetryPlanner:
    """First recovery policy: retry the same batch after a short fixed wait."""

    name = "naive-retry/v0"

    def __init__(self, backoff_ms: int = 50):
        self.backoff_ms = backoff_ms

    def decide(self, obs: Observation) -> Decision:
        option = _pick(obs, ActionKind.RETRY_CHUNK, ActionKind.RETRY_STEP, ActionKind.REPLAN, ActionKind.RUN_CHUNK, ActionKind.PAUSE)
        if option.kind in (ActionKind.RETRY_CHUNK, ActionKind.RETRY_STEP):
            batch = obs.batch_size if option.kind == ActionKind.RETRY_CHUNK else None
            return Decision(
                kind=option.kind,
                batch_size=batch,
                backoff_ms=self.backoff_ms,
                summary=f"Retry the same work after {self.backoff_ms} ms.",
                planner=self.name,
            )
        if option.kind == ActionKind.RUN_CHUNK:
            return Decision(
                kind=option.kind,
                batch_size=obs.batch_size,
                summary="Keep the current batch size.",
                planner=self.name,
            )
        return Decision(kind=option.kind, summary=f"Choose {option.kind.value}.", planner=self.name)


class AdaptivePlanner:
    """Recovery policy v1.

    Lock pressure: the chunk transaction is already rolled back. Shrink the
    batch so our own lock footprint is smaller, wait with exponential backoff
    (jitter seeded from the run id, so replays repeat), and retry from the
    same persisted cursor. After a streak of clean chunks, grow the batch
    back. When the retry budget is gone, pause with the checkpoint intact.
    """

    name = "adaptive/v1"

    def __init__(self, base_backoff_ms: int | None = None, shrink_factor: int | None = None):
        self.base_backoff_ms = base_backoff_ms
        self.shrink_factor = shrink_factor

    def backoff(self, obs: Observation, lo: int, hi: int, base: int) -> int:
        exp = base * (2 ** max(0, obs.retry_count - 1))
        jitter = zlib.crc32(f"{obs.run_id}:{obs.retry_count}".encode()) % (base // 4 + 1)
        return max(lo, min(hi, exp + jitter))

    def decide(self, obs: Observation) -> Decision:
        kinds = {o.kind: o for o in obs.options}
        err = obs.last_error
        if ActionKind.RETRY_CHUNK in kinds:
            opt = kinds[ActionKind.RETRY_CHUNK]
            lo_b, hi_b = opt.batch_size_range
            lo_w, hi_w = opt.backoff_ms_range
            base = self.base_backoff_ms or 400
            wait = self.backoff(obs, lo_w, hi_w, base)
            who = ""
            if err and err.blockers:
                b = max(err.blockers, key=lambda x: x.xact_age_ms or 0)
                who = f" Blocker pid {b.pid} holds {b.lock_mode} in a transaction {b.xact_age_ms} ms old."
            if err and err.error_class == ErrorClass.LOCK_PRESSURE:
                factor = self.shrink_factor or 4
                batch = max(lo_b, min(hi_b, obs.batch_size // factor))
                size = (
                    f"shrinking batch {obs.batch_size} -> {batch}"
                    if batch < obs.batch_size
                    else f"batch already at the {batch}-row floor"
                )
                return Decision(
                    kind=ActionKind.RETRY_CHUNK,
                    batch_size=batch,
                    backoff_ms=wait,
                    summary=(
                        f"Lock timeout at key {obs.cursor} (attempt {obs.retry_count}).{who} Chunk rolled back; "
                        f"{size}; waiting {wait} ms, then retrying from the same cursor."
                    ),
                    planner=self.name,
                )
            return Decision(
                kind=ActionKind.RETRY_CHUNK,
                batch_size=max(lo_b, min(hi_b, obs.batch_size)),
                backoff_ms=wait,
                summary=f"Transient error ({err.sqlstate if err else '?'}); retrying the same batch after {wait} ms.",
                planner=self.name,
            )
        if ActionKind.RETRY_STEP in kinds:
            lo_w, hi_w = kinds[ActionKind.RETRY_STEP].backoff_ms_range
            wait = self.backoff(obs, lo_w, hi_w, self.base_backoff_ms or 400)
            return Decision(kind=ActionKind.RETRY_STEP, backoff_ms=wait,
                            summary=f"DDL hit {err.error_class.value if err else 'an error'}; retry after {wait} ms.", planner=self.name)
        if ActionKind.RUN_CHUNK in kinds:
            lo_b, hi_b = kinds[ActionKind.RUN_CHUNK].batch_size_range
            return Decision(
                kind=ActionKind.RUN_CHUNK,
                batch_size=hi_b,
                summary=f"{obs.consecutive_successes} clean chunks in a row; growing batch {obs.batch_size} -> {hi_b}.",
                planner=self.name,
            )
        if ActionKind.REPLAN in kinds:
            return Decision(kind=ActionKind.REPLAN, summary="Schema changed without touching the migration's columns; rebuild the plan on the new baseline.", planner=self.name)
        if ActionKind.PAUSE in kinds:
            return Decision(kind=ActionKind.PAUSE, summary="Retry budget spent; pause and keep the checkpoint for an operator.", planner=self.name)
        return Decision(kind=obs.options[0].kind, summary=f"Only {obs.options[0].kind.value} is allowed.", planner=self.name)
