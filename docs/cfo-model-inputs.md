# cfo.ai model inputs

This sheet feeds the cfo.ai business model. Each number is either **measured** (from this repository's cost ledger, with its source) or an **assumption** (to be set and owned in cfo.ai). None of the assumptions is market data.

## Measured per run (local fixture, 10,000 rows)

Source: [`evidence/replay-adaptive-v1.json`](../evidence/replay-adaptive-v1.json), fields `db_ms`, `cost_usd`, `decisions`, `wall_ms`.

| Driver | Clean run | Run with lock recovery |
|---|---:|---:|
| Database time billed | 56 ms | 1,993 ms |
| Ledger cost at $0.50 per runner-hour | $0.0000077 | $0.0002767 |
| Planner decisions | 1 | 6 |
| Wall time | 0.36 s | 9.1 s (includes the 6 s seeded lock) |

These runs used the deterministic planner, so tokens are zero. With `--planner llm`, each decision is one Claude Opus 5.5 call, priced at $4 per million input tokens and $20 per million output tokens. Token counts per decision go in the cost ledger (`tokens_in`, `tokens_out`) and in the neatlogs trace. **To fill in:** measured tokens per decision from the first `--planner llm` replay.

Database cost grows with rows. The 100,000-row stress profile ([`evidence/replay-adaptive-v1-stress.json`](../evidence/replay-adaptive-v1-stress.json), `--profile stress`):

| Driver | Clean run | Run with lock recovery |
|---|---:|---:|
| Database time billed | 628 ms | 2,525 ms |
| Ledger cost | $0.0000873 | $0.0003505 |
| Batches | 24 | 35 |
| Planner decisions | 2 | 9 |
| Wall time | 2.4 s | 10.0 s |

Ten times the rows cost about eleven times the database time on a clean run (56 ms to 628 ms). Planner decisions grow much more slowly than rows (1 to 2 on a clean run, 6 to 9 with lock recovery) because the batch grows after clean streaks.

## Assumptions to set in cfo.ai

| Driver | Base | Upside | Downside | Note |
|---|---:|---:|---:|---|
| Price per database per month | $49 | $99 | $29 | Assumption; compare with migration-tool pricing |
| Databases per paying team | 3 | 6 | 2 | Assumption |
| Migrations per database per month | 4 | 8 | 2 | Assumption |
| Rows per migration | 1M | 10M | 100k | Assumption; scales database time |
| Planner decisions per migration | 6 | 12 | 4 | From replay: 1 clean, 6 with lock recovery (10k rows) |
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
