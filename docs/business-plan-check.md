# Business plan check

Computed by `scripts/plan_check.py` from the same drivers given to cfo.ai's Ari (see `cfo-ari-prompt.md`). Use it to check the cfo.ai model, not as a forecast. Starting cash ($50,000) and fixed monthly cost ($4,000) are placeholder assumptions.

## Unit economics per paying team per month

| | Base | Upside | Downside |
|---|---:|---:|---:|
| Revenue | $147.00 | $594.00 | $58.00 |
| Planner tokens | $0.05 | $0.19 | $0.02 |
| Database time | $0.02 | $0.08 | $0.01 |
| Hosting | $15.00 | $15.00 | $15.00 |
| Support | $78.00 | $58.80 | $132.00 |
| Cost to serve | $93.07 | $74.07 | $147.02 |
| Contribution | $53.93 | $519.93 | -$89.02 |
| Gross margin | 37% | 88% | -153% |
| Teams to break even | 75 | 8 | never |

## Scenario comparison

| Scenario | Month | Paying teams | MRR | Gross margin | Net result | Ending cash | Runway |
|---|---:|---:|---:|---:|---:|---:|---|
| Base | 12 | 102 | $15,002 | 37% | $1,504 | $39,769 | profitable |
| Base | 24 | 173 | $25,411 | 37% | $5,323 | $84,022 | profitable |
| Upside | 12 | 269 | $159,848 | 88% | $135,915 | $945,100 | profitable |
| Upside | 24 | 480 | $285,283 | 88% | $245,709 | $3,316,148 | profitable |
| Downside | 12 | 26 | $1,520 | -153% | -$6,333 | -$14,867 | out of cash |
| Downside | 24 | 39 | $2,243 | -153% | -$7,443 | -$98,888 | out of cash |

## Break-even month (first month with net ≥ 0)

- Base: month 9; lowest cash $36,116 in month 8
- Upside: month 1; lowest cash $58,998 in month 1
- Downside: not within 24 months; lowest cash -$98,888 in month 24; cash runs out in month 10

## Sensitivity (Base, gross margin at month 24)

- Base: 36.7%
- Tokens per decision doubled: 36.7%
- Rows per migration ×10: 36.6%
- Support 0.5 h per team: 57.1%
