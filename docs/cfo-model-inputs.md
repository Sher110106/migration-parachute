# cfo.ai model inputs

This sheet feeds the cfo.ai business model. Each number is either **measured** (from this repository's cost ledger, with its source) or an **assumption** (to be set and owned in cfo.ai). None of the assumptions is market data.

## Measured per run (local fixture, 10,000 rows)

Source: [`evidence/replay-adaptive-v1.json`](../evidence/replay-adaptive-v1.json), fields `db_ms`, `cost_usd`, `decisions`, `wall_ms`.

| Driver | Clean run | Run with lock recovery |
|---|---:|---:|
| Database time billed | 59 ms | 1,284 ms |
| Ledger cost at $0.50 per runner-hour | $0.0000082 | $0.0001782 |
| Planner decisions | 1 | 5 |
| Wall time | 0.34 s | 4.7 s |

These runs used the deterministic planner, so tokens are zero. With `--planner llm`, each decision is one Claude Opus 5.5 call, priced at $4 per million input tokens and $20 per million output tokens. Token counts per decision go in the cost ledger (`tokens_in`, `tokens_out`) and in the neatlogs trace. **To fill in:** measured tokens per decision from the first `--planner llm` replay.

Database cost grows with rows. The 100,000-row stress profile (`fixture.STRESS`) has not been measured yet.

## Assumptions to set in cfo.ai

| Driver | Base | Upside | Downside | Note |
|---|---:|---:|---:|---|
| Price per database per month | $49 | $99 | $29 | Assumption; compare with migration-tool pricing |
| Databases per paying team | 3 | 6 | 2 | Assumption |
| Migrations per database per month | 4 | 8 | 2 | Assumption |
| Rows per migration | 1M | 10M | 100k | Assumption; scales database time |
| Planner decisions per migration | 6 | 12 | 4 | From replay: 1 clean, 5 with lock recovery |
| Tokens per decision (in / out) | measure | measure | measure | From the first LLM replay |
| Hosting and storage per team per month | $15 | $25 | $10 | Assumption |
| Support hours per team per month | 1.0 | 0.5 | 2.0 | Assumption; on-call cost driver |
| Support cost per hour | $60 | $60 | $60 | Assumption |
| Failure rate needing human help | 5% | 2% | 10% | Replay suite: 0 false successes in 12 scenarios; real-world rate unknown |
| New paying teams per month | 10 | 25 | 3 | Assumption |
| Monthly churn | 3% | 2% | 6% | Assumption |
| Starting cash | set | set | set | Owner's figure |
| Fixed monthly cost (people, tools) | set | set | set | Owner's figure |

## Outputs the model should show

- Gross margin per team per month.
- Break-even number of paying teams.
- Runway under each scenario.
- How sensitive margin is to tokens per decision and to rows per migration.

## Status

Not yet entered. cfo.ai signup requires a card, followed by immediate cancellation under the event instructions, and the author must approve that step.
