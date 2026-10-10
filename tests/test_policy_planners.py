import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from parachute import policy
from parachute.domain import (
    ActionKind,
    ActionOption,
    Blocker,
    Decision,
    ErrorClass,
    ErrorEnvelope,
    Observation,
    Phase,
)
from parachute.llm_planner import ClaudePlanner, _schema
from parachute.planners import AdaptivePlanner, NaiveRetryPlanner

LOCK = ErrorEnvelope(
    error_class=ErrorClass.LOCK_PRESSURE,
    sqlstate="55P03",
    message="canceling statement due to lock timeout",
    retryable=True,
    blockers=(Blocker(pid=4242, lock_mode="RowShareLock", granted=True, xact_age_ms=900),),
)
RETRY_OPTIONS = (
    ActionOption(kind=ActionKind.RETRY_CHUNK, batch_size_range=(250, 2000), backoff_ms_range=(0, 5000)),
    ActionOption(kind=ActionKind.PAUSE),
    ActionOption(kind=ActionKind.ABORT),
)


def obs(options=RETRY_OPTIONS, retry_count=1, batch=2000, err=LOCK) -> Observation:
    return Observation(
        run_id="run_x",
        phase=Phase.BACKFILL_CHUNK_LOOP,
        cursor=4001,
        key_upper=10000,
        batch_size=batch,
        retry_count=retry_count,
        max_retries=5,
        consecutive_successes=0,
        last_error=err,
        rows_backfilled=4000,
        cost_usd="0",
        cost_cap_usd="0.5",
        options=options,
    )


def test_validate_rejects_unlisted_action():
    d = Decision(kind=ActionKind.APPLY_CONTRACT, summary="go", planner="t")
    assert "not allowed" in policy.validate(d, RETRY_OPTIONS)


def test_validate_rejects_out_of_bounds_batch_and_backoff():
    d = Decision(kind=ActionKind.RETRY_CHUNK, batch_size=10_000, backoff_ms=10, summary="x", planner="t")
    assert "batch_size" in policy.validate(d, RETRY_OPTIONS)
    d = Decision(kind=ActionKind.RETRY_CHUNK, batch_size=500, backoff_ms=99_999, summary="x", planner="t")
    assert "backoff_ms" in policy.validate(d, RETRY_OPTIONS)


def test_validate_requires_summary():
    d = Decision(kind=ActionKind.PAUSE, summary="  ", planner="t")
    assert "summary" in policy.validate(d, RETRY_OPTIONS)


def test_adaptive_shrinks_and_backs_off_exponentially():
    p = AdaptivePlanner()
    first = p.decide(obs(retry_count=1, batch=2000))
    second = p.decide(obs(retry_count=2, batch=500, options=(
        ActionOption(kind=ActionKind.RETRY_CHUNK, batch_size_range=(250, 500), backoff_ms_range=(0, 5000)),
        ActionOption(kind=ActionKind.PAUSE))))
    assert first.batch_size == 500 and second.batch_size == 250
    assert 400 <= first.backoff_ms < 800 <= second.backoff_ms
    assert "pid 4242" in first.summary
    assert policy.validate(first, RETRY_OPTIONS) is None


def test_adaptive_backoff_is_reproducible():
    assert AdaptivePlanner().decide(obs()).backoff_ms == AdaptivePlanner().decide(obs()).backoff_ms


def test_adaptive_pauses_when_only_pause_or_abort_left():
    d = AdaptivePlanner().decide(obs(options=(ActionOption(kind=ActionKind.PAUSE), ActionOption(kind=ActionKind.ABORT))))
    assert d.kind == ActionKind.PAUSE


def test_naive_retries_same_batch():
    d = NaiveRetryPlanner().decide(obs())
    assert d.kind == ActionKind.RETRY_CHUNK and d.batch_size == 2000 and d.backoff_ms == 50


def test_recovery_options_exhausted_budget():
    from parachute.state import apply
    from tests.test_state_store import started

    state = apply(None, started()).model_copy(update={"last_error": LOCK, "retry_count": 6})
    kinds = [o.kind for o in policy.chunk_recovery_options(state)]
    assert kinds == [ActionKind.PAUSE, ActionKind.ABORT]
    fatal = LOCK.model_copy(update={"error_class": ErrorClass.FATAL_SCHEMA})
    state = state.model_copy(update={"last_error": fatal, "retry_count": 1})
    assert [o.kind for o in policy.chunk_recovery_options(state)] == [ActionKind.FAIL]


# ---------------------------------------------------------------- Claude planner


class FakeMessages:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.behaviour(kwargs)


def fake_client(behaviour):
    msgs = FakeMessages(behaviour)
    return SimpleNamespace(messages=msgs, beta=SimpleNamespace(messages=msgs)), msgs


def answer(payload, stop="end_turn", tin=1200, tout=300):
    return lambda kwargs: SimpleNamespace(
        stop_reason=stop,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=tin, output_tokens=tout),
    )


def test_schema_enum_lists_only_allowed_actions():
    s = _schema(obs())
    assert s["properties"]["action"]["enum"] == ["abort", "pause", "retry_chunk"]
    assert s["additionalProperties"] is False


def test_claude_planner_parses_structured_answer_and_tokens():
    client, msgs = fake_client(answer({"action": "retry_chunk", "batch_size": 500, "backoff_ms": 1200,
                                       "summary": "55P03 from pid 4242; smaller batch, longer wait."}))
    d = ClaudePlanner(client).decide(obs())
    assert d.kind == ActionKind.RETRY_CHUNK and d.batch_size == 500 and d.backoff_ms == 1200
    assert d.tokens_in == 1200 and d.tokens_out == 300 and d.planner == "claude/claude-opus-5-5"
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in call  # always on for this model; never send disabled


def test_claude_planner_bedrock_has_no_server_fallback():
    client, msgs = fake_client(answer({"action": "pause", "batch_size": None, "backoff_ms": None, "summary": "Pause."}))
    ClaudePlanner(client, model="anthropic.claude-opus-5-5", first_party=False).decide(obs())
    assert "fallbacks" not in msgs.calls[0] and "betas" not in msgs.calls[0]


def test_claude_planner_degrades_on_refusal_and_api_errors():
    client, _ = fake_client(answer({}, stop="refusal"))
    d = ClaudePlanner(client).decide(obs())
    assert d.planner.startswith("adaptive/v1 (Claude unavailable: model declined")
    assert policy.validate(d, RETRY_OPTIONS) is None

    def boom(kwargs):
        raise anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))

    client, _ = fake_client(boom)
    d = ClaudePlanner(client).decide(obs())
    assert "connection error" in d.planner and d.kind == ActionKind.RETRY_CHUNK


def test_out_of_bounds_claude_answer_is_caught_by_policy():
    client, _ = fake_client(answer({"action": "retry_chunk", "batch_size": 50_000, "backoff_ms": 10, "summary": "Go big."}))
    d = ClaudePlanner(client).decide(obs())
    assert "batch_size" in policy.validate(d, RETRY_OPTIONS)


# ------------------------------------------------------------ OpenRouter planner

import openai as _openai

from parachute.llm_planner import OpenRouterPlanner


def or_client(behaviour):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return behaviour(kwargs)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def or_answer(payload, finish="stop", pin=871, pout=375):
    return lambda kwargs: SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=json.dumps(payload)))],
        usage=SimpleNamespace(prompt_tokens=pin, completion_tokens=pout),
    )


def test_openrouter_planner_sends_strict_schema_and_parses_tokens():
    client, calls = or_client(or_answer({"action": "retry_chunk", "batch_size": 500, "backoff_ms": 1000,
                                          "summary": "55P03 from pid 4242; smaller batch, longer wait."}))
    p = OpenRouterPlanner(client)
    d = p.decide(obs())
    assert d.kind == ActionKind.RETRY_CHUNK and d.batch_size == 500 and d.tokens_in == 871 and d.tokens_out == 375
    assert d.planner == "openrouter/deepseek/deepseek-v4.1-flash"
    rf = calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["properties"]["action"]["enum"] == ["abort", "pause", "retry_chunk"]
    assert calls[0]["extra_body"]["provider"]["require_parameters"] is True
    assert str(p.cost_model.input_per_mtok) == "0.30" and str(p.cost_model.output_per_mtok) == "1.20"


def test_openrouter_planner_degrades_on_truncation_and_errors():
    client, _ = or_client(or_answer({"action": "pause"}, finish="length"))
    d = OpenRouterPlanner(client).decide(obs())
    assert "answer hit max_tokens" in d.planner and policy.validate(d, RETRY_OPTIONS) is None

    def boom(kwargs):
        raise _openai.APIConnectionError(request=httpx2.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))

    client, _ = or_client(boom)
    d = OpenRouterPlanner(client).decide(obs())
    assert d.planner.startswith("adaptive/v1 (model unavailable: connection error")


def test_openrouter_malformed_answer_raises_for_the_orchestrator_to_record():
    bad = lambda kwargs: SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="not json"))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
    )
    client, _ = or_client(bad)
    with pytest.raises(ValueError):
        OpenRouterPlanner(client).decide(obs())
