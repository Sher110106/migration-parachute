"""HTTP API and static UI. One job at a time, because every scenario resets
the shared local fixture database."""

from __future__ import annotations

import json
import threading
import time
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from .fixture import DEFAULT_DSN
from .orchestrator import Orchestrator, Services
from .pg import PostgresAdapter
from .replay import SCENARIOS, make_tracer, run_scenario
from .state import RunState
from .store import ControlStore

WEB_DIR = Path(__file__).parent / "web"
EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "evidence"


def snapshot(state: RunState) -> dict[str, Any]:
    plan = state.plan
    return {
        "run_id": state.run_id,
        "mode": state.mode,
        "display_state": state.display_state,
        "phase": state.phase.value,
        "status": state.status.value,
        "revision": state.revision,
        "migration_id": state.spec.migration_id,
        "target": f"{state.spec.target_table}.{state.spec.change.old_column} -> {state.spec.change.new_column}",
        "lock_budget_ms": state.spec.lock_budget_ms,
        "max_retries": state.spec.max_retries,
        "plan": None
        if plan is None
        else {
            "summary": plan.summary,
            "plan_hash": state.plan_hash,
            "key_lower": plan.key_lower,
            "key_upper": plan.key_upper,
            "estimated_rows": plan.estimated_rows,
            "initial_batch": plan.initial_batch,
            "expand": [{"name": a.name, "statements": list(a.statements), "tx_mode": a.tx_mode} for a in plan.expand],
            "contract": [{"name": a.name, "statements": list(a.statements)} for a in plan.contract],
            "rollback": [{"name": a.name, "statements": list(a.statements)} for a in plan.rollback_expand],
        },
        "replans": state.replans,
        "cursor": state.cursor,
        "key_upper": state.key_upper,
        "batch_size": state.batch_size,
        "rows_backfilled": state.rows_backfilled,
        "chunks": [c.model_dump() for c in state.chunks],
        "retry_count": state.retry_count,
        "total_retries": state.total_retries,
        "catch_ups": state.catch_ups,
        "lock_wait_ms": state.lock_wait_ms,
        "db_ms": state.db_ms,
        "tokens_in": state.tokens_in,
        "tokens_out": state.tokens_out,
        "cost_usd": str(state.cost_usd),
        "cost_cap_usd": str(state.cost_cap_usd),
        "decisions": state.decisions,
        "rejected_decisions": state.rejected_decisions,
        "last_decision": state.last_decision.model_dump(mode="json") if state.last_decision else None,
        "last_error": state.last_error.model_dump(mode="json") if state.last_error else None,
        "pause_reason": state.pause_reason,
        "verifications": {k: v.model_dump(mode="json") for k, v in state.verifications.items()},
        "contract": {
            "sql_hash": state.contract_sql_hash,
            "statements": list(state.contract_statements),
            "rollback_statements": list(state.rollback_statements),
            "dependencies": list(state.contract_dependencies),
            "proof_hash": state.contract_proof_hash,
            "approval_id": state.approval_id,
            "approved_by": state.approved_by,
        },
        "final_proof_hash": state.final_proof_hash,
        "started_at": state.started_at,
        "updated_at": state.updated_at,
    }


class StartRun(BaseModel):
    scenario: str
    planner: str = "adaptive"


class Approve(BaseModel):
    approval_id: str
    approver: str = "demo-owner"


class Resume(BaseModel):
    cost_cap_usd: Decimal | None = None


def create_app(dsn: str = DEFAULT_DSN, var_dir: Path = Path("var"), planners: dict[str, Callable] | None = None) -> FastAPI:
    from . import planners as planner_mod

    factories: dict[str, Callable] = planners or {
        "adaptive": planner_mod.AdaptivePlanner,
        "naive": planner_mod.NaiveRetryPlanner,
    }
    try:
        from .llm_planner import llm_factory

        if (f := llm_factory()) is not None:
            factories.setdefault("llm", f)
    except ImportError:
        pass

    store = ControlStore(var_dir / "control.sqlite")
    app = FastAPI(title="Migration Parachute")
    job: dict[str, Any] = {"busy": False, "kind": None, "run_id": None, "error": None, "result": None}
    results: dict[str, dict] = {}
    lock = threading.Lock()

    def launch(kind: str, run_id: str | None, fn: Callable[[], Any]) -> None:
        with lock:
            if job["busy"]:
                raise HTTPException(409, f"a {job['kind']} job is running ({job['run_id']})")
            job.update(busy=True, kind=kind, run_id=run_id, error=None, result=None)

        def worker():
            try:
                job["result"] = fn()
            except Exception as exc:  # surfaced through /api/job
                job["error"] = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                job["busy"] = False

        threading.Thread(target=worker, daemon=True, name=f"job-{kind}").start()

    def services(planner_name: str = "adaptive") -> Services:
        return Services(store=store, db=PostgresAdapter(dsn), planner=factories[planner_name](), tracer=make_tracer(var_dir))

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/api/meta")
    def meta():
        return {
            "planners": sorted(factories),
            "scenarios": [{"id": s.id, "description": s.description, "expect": list(s.expect), "faults": sorted(s.faults),
                           "mode": s.mode} for s in SCENARIOS.values()],
            "fixture": "Fictional local fixture: 2,000 customers and 10,000 orders on local Postgres 15.",
        }

    @app.get("/api/job")
    def job_status():
        return {k: job[k] for k in ("busy", "kind", "run_id", "error")}

    @app.post("/api/runs")
    def start(body: StartRun):
        if body.scenario not in SCENARIOS:
            raise HTTPException(404, "unknown scenario")
        if body.planner not in factories:
            raise HTTPException(404, "unknown planner")
        run_id = f"{body.scenario}_{int(time.time() * 1000) % 10_000_000:07d}"

        def go():
            res = run_scenario(SCENARIOS[body.scenario], factories[body.planner], dsn=dsn, var_dir=var_dir, store=store,
                               run_id=run_id)
            results[run_id] = res
            return res

        launch("start", run_id, go)
        return {"run_id": run_id}

    @app.get("/api/runs")
    def list_runs():
        return store.runs()

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        try:
            state = store.materialized(run_id)
        except KeyError:
            raise HTTPException(404, "no such run")
        out = snapshot(state)
        out["result"] = results.get(run_id)
        out["chain_ok"] = store.verify_chain(run_id)
        out["checkpoint_ref"] = store.latest_checkpoint_ref(run_id)
        return out

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0):
        return store.event_dicts(run_id, after)

    @app.get("/api/runs/{run_id}/trace")
    def trace(run_id: str):
        path = var_dir / "traces" / f"{run_id}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    @app.post("/api/runs/{run_id}/approve")
    def approve(run_id: str, body: Approve):
        launch("approve", run_id, lambda: snapshot(Orchestrator(services()).approve_contract(run_id, body.approval_id, body.approver)))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/resume")
    def resume(run_id: str, body: Resume):
        launch("resume", run_id, lambda: snapshot(Orchestrator(services()).resume_run(run_id, new_cost_cap_usd=body.cost_cap_usd)))
        return {"ok": True}

    @app.post("/api/runs/{run_id}/abort")
    def abort(run_id: str):
        launch("abort", run_id, lambda: snapshot(Orchestrator(services()).abort_run(run_id, "operator abort from UI")))
        return {"ok": True}

    @app.get("/api/evidence")
    def evidence():
        out = {}
        for name in ("replay-naive-v0.json", "replay-adaptive-v1.json"):
            p = EVIDENCE_DIR / name
            if p.exists():
                out[name.removesuffix(".json")] = json.loads(p.read_text())
        return JSONResponse(out)

    return app
