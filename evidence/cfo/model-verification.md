# cfo.ai model verification

The cfo.ai workspace page is [Migration Parachute: business plan](https://app.cfo.ai/sher-cfoai-a1e2b615/pages/a8fa343a-4234-4a54-a784-971b110829a8).

The page contains measured and assumption driver tables, Base/Upside/Downside scenarios, a month-12/month-24 comparison, break-even, unit economics, monthly plan, and sensitivity sections. Its visible outputs match the independent checker in [`docs/business-plan-check.md`](../../docs/business-plan-check.md):

| Scenario | Month 12 teams | Month 12 MRR | Month 12 gross margin | Month 24 teams | Month 24 MRR | Month 24 ending cash |
|---|---:|---:|---:|---:|---:|---:|
| Base | 102.053 | $15,001.72 | 36.7% | 172.861 | $25,410.56 | $84,021.77 |
| Upside | 269.104 | $159,847.83 | 87.5% | 480.275 | $285,283.10 | $3,316,147.58 |
| Downside | 26.204 | $1,519.83 | -153.5% | 38.675 | $2,243.15 | -$98,887.56 |

The independent checker uses `ceil(fixed_monthly / contribution)` for whole-team break-even. Base therefore requires 75 whole teams; Upside requires 8; Downside has no positive break-even contribution. Base first reaches monthly break-even in month 9, Upside in month 1, and Downside never reaches it in 24 months.

The cfo.ai page treats token and runner prices, hosting, support, growth, churn, starting cash, and fixed costs as assumptions unless measured by the repository evidence. Model numbers are planning inputs, not validated market data. The page is currently account-scoped; verify external read-only access before submitting the link to judges.
