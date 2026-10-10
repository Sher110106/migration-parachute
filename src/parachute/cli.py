"""Command line: replay scenarios, run one, resume, approve, serve the UI."""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from .fixture import DEFAULT_DSN
from .replay import MINIMUM_SUITE, SCENARIOS, run_suite


def planner_factory(name: str):
    from . import planners

    if name == "naive":
        return planners.NaiveRetryPlanner
    if name == "adaptive":
        return planners.AdaptivePlanner
    if name == "llm":
        from .llm_planner import llm_factory

        factory = llm_factory()
        if factory is None:
            raise SystemExit("no LLM provider configured: set ANTHROPIC_API_KEY, or PARACHUTE_LLM=bedrock with AWS_PROFILE")
        return factory
    raise SystemExit(f"unknown planner {name!r}")


def main(argv: list[str] | None = None) -> int:
    from .envfile import load_env

    load_env()
    ap = argparse.ArgumentParser(prog="parachute")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--var", default="var", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="run scenarios against a fresh fixture each time")
    rp.add_argument("scenarios", nargs="*", help=f"ids (default: all). Known: {', '.join(SCENARIOS)}")
    rp.add_argument("--minimum", action="store_true", help="run the minimum adversarial suite")
    rp.add_argument("--planner", default="adaptive", choices=["adaptive", "naive", "llm"])
    rp.add_argument("--out", type=Path, default=None)
    rp.add_argument("--profile", choices=["demo", "stress"], default=None, help="override the fixture size")

    sv = sub.add_parser("serve", help="serve the execution UI and API")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)

    args = ap.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(dsn=args.dsn, var_dir=args.var), host=args.host, port=args.port, log_level="warning")
        return 0
    if args.cmd == "replay":
        ids = list(MINIMUM_SUITE) if args.minimum else (args.scenarios or list(SCENARIOS))
        out = args.out or args.var / "results" / f"replay-{args.planner}.json"
        profile = None
        if args.profile:
            from .fixture import DEMO, STRESS

            profile = STRESS if args.profile == "stress" else DEMO
        results = run_suite(ids, planner_factory(args.planner), out, dsn=args.dsn, var_dir=args.var, profile=profile)
        passed = sum(r["passed"] for r in results)
        print(f"\n{passed}/{len(results)} scenarios behaved as expected -> {out}")
        return 0 if passed == len(results) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
