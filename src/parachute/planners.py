"""Planners choose one bounded action from the options policy allows."""

from __future__ import annotations

from typing import Protocol

from .domain import ActionKind, Decision, Observation


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
