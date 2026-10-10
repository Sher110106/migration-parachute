"""Claude as the recovery planner, inside the policy fence.

Claude sees the observation (phase, cursor, batch, retry budget, the last
error with blocker evidence, cost) and the options policy allows right now.
It answers with one action in a JSON schema whose ``action`` enum holds only
those options. Policy still checks every bound; out-of-bounds answers are
rejected and recorded. If the model cannot be reached, the deterministic
planner answers and the decision says so.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

import anthropic

from .domain import ActionKind, Decision, Observation
from .planners import AdaptivePlanner, Planner

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You are the recovery planner inside Migration Parachute, an agent that runs an \
expand / backfill / contract migration on a live Postgres table. The orchestrator \
asks you only at decision points. You choose one action; the orchestrator runs it.

How the system works:
- Backfill runs in key-range chunks. Each chunk is one short transaction with a \
lock_timeout. A failed chunk rolls back completely, so a lock timeout loses no work; \
the cursor stays where it was and is persisted.
- SQLSTATE 55P03 means another session holds row locks we need. The error lists the \
sessions that held locks on the table when we failed, with their transaction age. \
A smaller batch shortens how long our own transaction holds locks and touches fewer \
hot rows. Waiting longer gives the other session time to finish.
- Retries are capped by max_retries. When the budget is spent, pausing keeps the \
checkpoint for an operator. Abort removes the additive objects and stops.
- You never write SQL, never approve destructive steps, and cannot widen any bound. \
A policy check rejects any action not in `options` or any value outside its range.

Answer rules:
- Choose exactly one action from `options`.
- If the chosen option has batch_size_range, give batch_size inside it; otherwise null.
- If the chosen option has backoff_ms_range, give backoff_ms inside it; otherwise null.
- summary: one or two plain sentences for the on-call engineer: what you saw (cite \
SQLSTATE, blocker pid and transaction age, cursor, retries used), what you chose, why.
"""


def _schema(obs: Observation) -> dict[str, Any]:
    kinds = sorted({o.kind.value for o in obs.options})
    nullable_int = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": kinds},
            "batch_size": nullable_int,
            "backoff_ms": nullable_int,
            "summary": {"type": "string"},
        },
        "required": ["action", "batch_size", "backoff_ms", "summary"],
        "additionalProperties": False,
    }


class PlannerUnavailable(RuntimeError):
    pass


class ClaudePlanner:
    """Calls Claude through the Anthropic SDK (first-party API or Bedrock)."""

    def __init__(
        self,
        client: Any,
        model: str = DEFAULT_MODEL,
        effort: str = "medium",
        first_party: bool = True,
        fallback: Planner | None = None,
    ):
        self.client = client
        self.model = model
        self.effort = effort
        self.first_party = first_party
        self.fallback = fallback or AdaptivePlanner()
        self.name = f"claude/{model}"

    def _request(self, obs: Observation):
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(obs.model_dump(mode="json"), indent=1)}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": _schema(obs)}},
        )
        if self.first_party:
            # Server-side refusal fallback; not offered on Bedrock.
            return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
        return self.client.messages.create(**kwargs)

    def decide(self, obs: Observation) -> Decision:
        try:
            resp = self._request(obs)
        except anthropic.APIConnectionError as exc:
            return self._degrade(obs, f"connection error: {exc.__class__.__name__}")
        except anthropic.RateLimitError:
            return self._degrade(obs, "rate limited")
        except anthropic.APIStatusError as exc:
            return self._degrade(obs, f"API status {exc.status_code}")
        if resp.stop_reason == "refusal":
            return self._degrade(obs, "model declined the request")
        if resp.stop_reason == "max_tokens":
            return self._degrade(obs, "answer hit max_tokens")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            return self._degrade(obs, "no text block in the answer")
        data = json.loads(text)
        return Decision(
            kind=ActionKind(data["action"]),
            batch_size=data.get("batch_size"),
            backoff_ms=data.get("backoff_ms"),
            summary=str(data.get("summary", "")).strip(),
            planner=self.name,
            tokens_in=int(resp.usage.input_tokens or 0),
            tokens_out=int(resp.usage.output_tokens or 0),
        )

    def _degrade(self, obs: Observation, why: str) -> Decision:
        d = self.fallback.decide(obs)
        return d.model_copy(
            update={"planner": f"{self.fallback.name} (Claude unavailable: {why})", "summary": d.summary}
        )


def _maybe_wrap(client: Any) -> Any:
    if not os.environ.get("NEATLOGS_API_KEY"):
        return client
    try:
        import neatlogs

        return neatlogs.wrap(client)
    except Exception:
        return client


def llm_factory() -> Callable[[], ClaudePlanner] | None:
    """Build a planner factory from the environment, or None if no provider is configured.

    PARACHUTE_LLM=anthropic  uses the Anthropic API (ANTHROPIC_API_KEY or an `ant` profile).
    PARACHUTE_LLM=bedrock    uses Amazon Bedrock through the Mantle client
                             (AWS_PROFILE / AWS_REGION, model prefix ``anthropic.``).
    Unset: Anthropic if ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set, else None.
    """
    provider = os.environ.get("PARACHUTE_LLM", "").lower()
    if not provider and (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        provider = "anthropic"
    model = os.environ.get("PARACHUTE_LLM_MODEL", DEFAULT_MODEL)
    effort = os.environ.get("PARACHUTE_LLM_EFFORT", "medium")
    if provider == "anthropic":
        return lambda: ClaudePlanner(_maybe_wrap(anthropic.Anthropic()), model=model, effort=effort, first_party=True)
    if provider == "bedrock":
        region = os.environ.get("AWS_REGION", "ap-south-1")
        profile = os.environ.get("AWS_PROFILE")
        bedrock_model = model if model.startswith("anthropic.") else f"anthropic.{model}"
        return lambda: ClaudePlanner(
            _maybe_wrap(anthropic.AnthropicBedrockMantle(aws_region=region, aws_profile=profile)),
            model=bedrock_model,
            effort=effort,
            first_party=False,
        )
    return None
