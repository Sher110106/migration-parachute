# Prompt for Ari (cfo.ai)

Paste the block below into Ari in cfo.ai. It builds the model from stated numbers only. Measured values come from this repository's cost ledger (see [`cfo-model-inputs.md`](cfo-model-inputs.md)). Everything else is an assumption, labelled as such; change any of them in cfo.ai afterwards.

Starting cash ($50,000) and fixed monthly cost ($4,000) are placeholder assumptions; change them in cfo.ai if you have real figures. [`business-plan-check.md`](business-plan-check.md) recomputes the same plan with `scripts/plan_check.py`, so Ari's numbers can be checked.

---

```text
Build a 24-month financial model for "Migration Parachute", a hosted agent that runs Postgres schema migrations (expand, chunked backfill, verified contract) for engineering teams. There is no revenue yet; this is a plan. Use only the numbers below, keep every input as an editable driver, and label each one as Measured or Assumption.

Pricing and volume (Assumptions)
- Price: $49 per connected database per month.
- Databases per paying team: 3.
- Migrations per database per month: 4.
- Rows per migration: 1,000,000.
- New paying teams per month: 10, starting month 1.
- Monthly churn of paying teams: 3%.

Variable cost per migration
- Planner decisions per migration: 6 (Measured on a 10,000-row test run with one lock-recovery episode; a clean run used 1).
- Planner model tokens per decision: 663 input and 374 output (Measured, DeepSeek V4.1 Flash via OpenRouter, 14 decisions). Price $0.30 per million input tokens and $1.20 per million output tokens.
- Database runner time: 6.3 seconds per million rows on a clean run (Measured: 628 ms per 100,000 rows), priced at $0.50 per runner-hour. Double it to allow for lock recovery.

Other costs (Assumptions)
- Hosting and storage: $15 per paying team per month.
- Support: 1 hour per paying team per month at $60 per hour.
- Share of migrations that need human help: 5%, at 0.5 support hours each, on top of the above.
- Fixed monthly cost: $4,000 per month ($3,000 founder stipend, $500 tools, $500 infrastructure baseline). Placeholder assumption.
- Starting cash: $50,000. Placeholder assumption.

Outputs I need
1. Monthly revenue, cost of revenue (tokens + database time + hosting + support), gross profit and gross margin %.
2. Unit economics per paying team per month: revenue, cost to serve, contribution margin.
3. Operating expenses, net burn, ending cash and runway in months.
4. Break-even month and the number of paying teams needed to break even.
5. Three scenarios branched from Main:
   - Base: the values above.
   - Upside: price $99, 6 databases per team, 8 migrations per database, 25 new teams per month, churn 2%, support 0.5 hours per team, 2% of migrations need help.
   - Downside: price $29, 2 databases per team, 2 migrations per database, 3 new teams per month, churn 6%, support 2 hours per team, 10% of migrations need help.
6. A table that compares the three scenarios side by side for month 12 and month 24: paying teams, MRR, gross margin %, net burn, ending cash, runway.
7. A sensitivity view: gross margin % as tokens per decision double and as rows per migration grow tenfold.

Put the results on one Page titled "Migration Parachute: business plan" with the scenario comparison table at the top. Do not add any number I have not given you; if something is missing, ask me.
```

---

After Ari builds it:

1. Check that every driver above appears and is marked Measured or Assumption.
2. Share the Page with a read-only link (Share on the Page; pick the Base scenario, or one link per scenario) and open the link in a signed-out window to confirm it loads.
3. Download the Page's tables as a workbook and save it under `evidence/cfo/`.
4. Add the share link to the README.

The neatHack page says to cancel the cfo.ai subscription in Settings → Billing right after signing up, and that the event team provides credits. cfo.ai's docs add that if the trial's AI usage runs out, a "Start paid plan" button appears, and confirming it charges the card on file. Do not press it.
