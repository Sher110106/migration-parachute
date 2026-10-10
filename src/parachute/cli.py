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
    raise SystemExit(f"unknown planner {name!r}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parachute")
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--var", default="var", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="run scenarios against a fresh fixture each time")
    rp.add_argument("scenarios", nargs="*", help=f"ids (default: all). Known: {', '.join(SCENARIOS)}")
    rp.add_argument("--minimum", action="store_true", help="run the minimum adversarial suite")
    rp.add_argument("--planner", default="naive")
    rp.add_argument("--out", type=Path, default=None)

    args = ap.parse_args(argv)
    if args.cmd == "replay":
        ids = list(MINIMUM_SUITE) if args.minimum else (args.scenarios or list(SCENARIOS))
        out = args.out or args.var / "results" / f"replay-{args.planner}.json"
        results = run_suite(ids, planner_factory(args.planner), out, dsn=args.dsn, var_dir=args.var)
        passed = sum(r["passed"] for r in results)
        print(f"\n{passed}/{len(results)} scenarios behaved as expected -> {out}")
        return 0 if passed == len(results) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
