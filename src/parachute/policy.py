"""What the agent may do next, and the check every planner decision must pass.

Planners choose; policy decides what is allowed. A decision outside the
listed options or their bounds is rejected and recorded, never executed.
"""

from __future__ import annotations

from .domain import (
    ActionKind,
    ActionOption,
    Decision,
    ErrorClass,
    ErrorEnvelope,
    RETRYABLE,
)
from .state import RunState


def chunk_recovery_options(state: RunState) -> tuple[ActionOption, ...]:
    cp = state.spec.chunk_policy
    err = state.last_error
    if err is None:
        return ()
    if err.error_class in RETRYABLE and state.retry_count <= state.spec.max_retries:
        return (
            ActionOption(
                kind=ActionKind.RETRY_CHUNK,
                batch_size_range=(cp.min_batch, max(cp.min_batch, state.batch_size)),
                backoff_ms_range=(0, cp.max_backoff_ms),
                note=f"retry {state.retry_count} of {state.spec.max_retries} from cursor {state.cursor}",
            ),
            ActionOption(kind=ActionKind.PAUSE, note="keep the checkpoint and wait for an operator"),
            ActionOption(kind=ActionKind.ABORT, note="remove additive objects and stop"),
        )
    if err.error_class in RETRYABLE:
        return (
            ActionOption(kind=ActionKind.PAUSE, note="retry budget exhausted"),
            ActionOption(kind=ActionKind.ABORT, note="remove additive objects and stop"),
        )
    return (ActionOption(kind=ActionKind.FAIL, note=f"{err.error_class.value} is not retryable"),)


def step_recovery_options(state: RunState, error: ErrorEnvelope, attempt: int) -> tuple[ActionOption, ...]:
    cp = state.spec.chunk_policy
    if error.error_class in RETRYABLE and attempt <= state.spec.max_retries:
        return (
            ActionOption(kind=ActionKind.RETRY_STEP, backoff_ms_range=(0, cp.max_backoff_ms)),
            ActionOption(kind=ActionKind.PAUSE),
            ActionOption(kind=ActionKind.ABORT),
        )
    if error.error_class in RETRYABLE:
        return (ActionOption(kind=ActionKind.PAUSE), ActionOption(kind=ActionKind.ABORT))
    return (ActionOption(kind=ActionKind.FAIL, note=f"{error.error_class.value} is not retryable"),)


def chunk_progress_options(state: RunState) -> tuple[ActionOption, ...]:
    cp = state.spec.chunk_policy
    if state.consecutive_successes >= cp.grow_after_successes and state.batch_size < cp.max_batch:
        return (
            ActionOption(
                kind=ActionKind.RUN_CHUNK,
                batch_size_range=(state.batch_size, min(cp.max_batch, state.batch_size * 2)),
                note=f"{state.consecutive_successes} clean chunks in a row",
            ),
        )
    return (ActionOption(kind=ActionKind.RUN_CHUNK, batch_size_range=(state.batch_size, state.batch_size)),)


def drift_options(conflict: bool) -> tuple[ActionOption, ...]:
    if conflict:
        return (ActionOption(kind=ActionKind.FAIL, note="the target column already exists with another shape"),)
    return (
        ActionOption(kind=ActionKind.REPLAN, note="inspect again and rebuild the plan on the new baseline"),
        ActionOption(kind=ActionKind.FAIL),
    )


def is_forced(options: tuple[ActionOption, ...]) -> bool:
    if len(options) != 1:
        return False
    o = options[0]
    fixed_batch = o.batch_size_range is None or o.batch_size_range[0] == o.batch_size_range[1]
    return fixed_batch and o.backoff_ms_range is None


def validate(decision: Decision, options: tuple[ActionOption, ...]) -> str | None:
    match = next((o for o in options if o.kind == decision.kind), None)
    if match is None:
        allowed = ", ".join(o.kind.value for o in options)
        return f"{decision.kind.value} is not allowed here (allowed: {allowed})"
    if match.batch_size_range is not None:
        lo, hi = match.batch_size_range
        if decision.batch_size is None or not lo <= decision.batch_size <= hi:
            return f"batch_size {decision.batch_size} outside [{lo}, {hi}]"
    elif decision.batch_size is not None:
        return "batch_size is not a parameter of this action"
    if match.backoff_ms_range is not None:
        lo, hi = match.backoff_ms_range
        if decision.backoff_ms is None or not lo <= decision.backoff_ms <= hi:
            return f"backoff_ms {decision.backoff_ms} outside [{lo}, {hi}]"
    if not decision.summary.strip():
        return "a decision needs a summary"
    return None


def forced_decision(option: ActionOption, summary: str) -> Decision:
    batch = option.batch_size_range[0] if option.batch_size_range else None
    return Decision(kind=option.kind, batch_size=batch, summary=summary, planner="policy")


def error_is(err: ErrorEnvelope | None, *classes: ErrorClass) -> bool:
    return err is not None and err.error_class in classes
