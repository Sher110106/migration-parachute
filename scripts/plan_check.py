"""Independent check of the cfo.ai business model.

Recomputes the 24-month plan from the same drivers given to Ari, so the
numbers cfo.ai produces can be checked rather than trusted. Measured
drivers come from evidence/; every other driver is an assumption.

    uv run python scripts/plan_check.py > docs/business-plan-check.md
"""

from __future__ import annotations

from dataclasses import dataclass, replace

MONTHS = 24


@dataclass(frozen=True)
class Drivers:
    # Assumptions
    price_per_db: float = 49.0
    dbs_per_team: float = 3
    migrations_per_db: float = 4
    rows_per_migration: float = 1_000_000
    new_teams_per_month: float = 10
    churn: float = 0.03
    hosting_per_team: float = 15.0
    support_hours_per_team: float = 1.0
    support_rate: float = 60.0
    help_rate: float = 0.05
    help_hours: float = 0.5
    fixed_monthly: float = 4_000.0
    starting_cash: float = 50_000.0
    # Measured (evidence/replay-llm-deepseek-v4.1-flash.json, replay-adaptive-v1-stress.json)
    decisions_per_migration: float = 6
    tokens_in: float = 663
    tokens_out: float = 374
    price_in_per_mtok: float = 0.30
    price_out_per_mtok: float = 1.20
    db_seconds_per_mrows: float = 6.3 * 2  # doubled for lock recovery
    runner_per_hour: float = 0.50


BASE = Drivers()
UPSIDE = replace(BASE, price_per_db=99, dbs_per_team=6, migrations_per_db=8, new_teams_per_month=25, churn=0.02,
                 support_hours_per_team=0.5, help_rate=0.02)
DOWNSIDE = replace(BASE, price_per_db=29, dbs_per_team=2, migrations_per_db=2, new_teams_per_month=3, churn=0.06,
                   support_hours_per_team=2.0, help_rate=0.10)


def per_team(d: Drivers) -> dict[str, float]:
    migrations = d.dbs_per_team * d.migrations_per_db
    token = migrations * d.decisions_per_migration * (
        d.tokens_in * d.price_in_per_mtok + d.tokens_out * d.price_out_per_mtok) / 1e6
    db = migrations * (d.rows_per_migration / 1e6) * d.db_seconds_per_mrows * d.runner_per_hour / 3600
    support = d.support_hours_per_team * d.support_rate + migrations * d.help_rate * d.help_hours * d.support_rate
    revenue = d.price_per_db * d.dbs_per_team
    cost = token + db + d.hosting_per_team + support
    return {"revenue": revenue, "tokens": token, "db": db, "hosting": d.hosting_per_team, "support": support,
            "cost": cost, "contribution": revenue - cost}


def simulate(d: Drivers) -> list[dict[str, float]]:
    unit = per_team(d)
    teams, cash, rows = 0.0, d.starting_cash, []
    for m in range(1, MONTHS + 1):
        teams = teams * (1 - d.churn) + d.new_teams_per_month
        revenue = teams * unit["revenue"]
        cogs = teams * unit["cost"]
        net = revenue - cogs - d.fixed_monthly
        cash += net
        rows.append({"month": m, "teams": teams, "mrr": revenue, "cogs": cogs, "gross_margin": (revenue - cogs) / revenue,
                     "net": net, "cash": cash})
    return rows


def money(x: float, cents: bool = False) -> str:
    text = f"${abs(x):,.2f}" if cents else f"${abs(x):,.0f}"
    return f"-{text}" if x < 0 else text


def runway(rows: list[dict[str, float]], m: int) -> str:
    r = rows[m - 1]
    if r["net"] >= 0:
        return "profitable"
    return f"{r['cash'] / -r['net']:.1f} mo" if r["cash"] > 0 else "out of cash"


def main() -> None:
    scenarios = {"Base": BASE, "Upside": UPSIDE, "Downside": DOWNSIDE}
    print("# Business plan check\n")
    print("Computed by `scripts/plan_check.py` from the same drivers given to cfo.ai's Ari "
          "(see `cfo-ari-prompt.md`). Use it to check the cfo.ai model, not as a forecast. "
          "Starting cash ($50,000) and fixed monthly cost ($4,000) are placeholder assumptions.\n")
    print("## Unit economics per paying team per month\n")
    print("| | Base | Upside | Downside |\n|---|---:|---:|---:|")
    units = {k: per_team(v) for k, v in scenarios.items()}
    for key, label in [("revenue", "Revenue"), ("tokens", "Planner tokens"), ("db", "Database time"),
                       ("hosting", "Hosting"), ("support", "Support"), ("cost", "Cost to serve"),
                       ("contribution", "Contribution")]:
        print(f"| {label} | " + " | ".join(money(units[k][key], cents=True) for k in scenarios) + " |")
    print("| Gross margin | " + " | ".join(f"{u['contribution'] / u['revenue']:.0%}" for u in units.values()) + " |")
    print("| Teams to break even | " + " | ".join(
        f"{BASE.fixed_monthly / u['contribution']:.0f}" if u["contribution"] > 0 else "never" for u in units.values()) + " |")
    print("\n## Scenario comparison\n")
    print("| Scenario | Month | Paying teams | MRR | Gross margin | Net result | Ending cash | Runway |")
    print("|---|---:|---:|---:|---:|---:|---:|---|")
    for name, d in scenarios.items():
        rows = simulate(d)
        for m in (12, 24):
            r = rows[m - 1]
            print(f"| {name} | {m} | {r['teams']:.0f} | {money(r['mrr'])} | {r['gross_margin']:.0%} | {money(r['net'])} | "
                  f"{money(r['cash'])} | {runway(rows, m)} |")
    print("\n## Break-even month (first month with net ≥ 0)\n")
    for name, d in scenarios.items():
        rows = simulate(d)
        be = next((r["month"] for r in rows if r["net"] >= 0), None)
        low = min(rows, key=lambda r: r["cash"])
        broke = next((r["month"] for r in rows if r["cash"] < 0), None)
        tail = f"; cash runs out in month {broke}" if broke else ""
        print(f"- {name}: {f'month {be}' if be else 'not within 24 months'}; lowest cash {money(low['cash'])} "
              f"in month {low['month']}{tail}")
    print("\n## Sensitivity (Base, gross margin at month 24)\n")
    for label, d in [("Base", BASE), ("Tokens per decision doubled", replace(BASE, tokens_in=1326, tokens_out=748)),
                     ("Rows per migration ×10", replace(BASE, rows_per_migration=10_000_000)),
                     ("Support 0.5 h per team", replace(BASE, support_hours_per_team=0.5))]:
        print(f"- {label}: {simulate(d)[-1]['gross_margin']:.1%}")


if __name__ == "__main__":
    main()
