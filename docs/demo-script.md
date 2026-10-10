# Demo script (target 2:40, limit 3:00)

Recorded artifact: [`artifacts/demo/execution-ui-replay.mp4`](../artifacts/demo/execution-ui-replay.mp4), 179 seconds. The recording uses the persisted adaptive run `lock_timeout_1137893`; the evidence-summary cut is [`artifacts/demo/migration-parachute-demo.mp4`](../artifacts/demo/migration-parachute-demo.mp4).

The rules ask the video to cover architecture, implementation, runtime, reasoning and result for neatlogs, Entire and cfo.ai. Each segment below names which of those it shows.

| Time | Screen | Say |
|---|---|---|
| 0:00–0:12 | Execution UI, header with "Fictional local fixture" | "A schema migration can be correct and still take down a service. This agent runs one on a live table and recovers when it gets in someone's way." |
| 0:12–0:25 | Plan panel: expand, backfill, contract SQL | "The task: move 10,000 orders from a customer email to a customer ID. It plans expand, chunked backfill, and a contract step it may not run on its own." |
| 0:25–0:40 | Start `baseline_lock_timeout` | "First the naive way: one UPDATE. Another session holds 100 rows. Lock timeout, everything rolls back, zero rows kept. It does not claim success." |
| 0:40–1:25 | Start `lock_timeout` with `adaptive`; key-range bar, decisions panel, blocker card | "Now the agent. Chunk 3 hits the lock. It rolls back that chunk only, records the blocking session and its age, shrinks the batch from 2,000 to 500, then 250, and waits longer each time. Every decision comes from a short list that policy allows, and an answer outside those bounds is rejected. The fourth retry goes through, the batch grows back, and the backfill finishes from the same cursor." |
| 1:25–1:42 | Verification card, contract gate | "Parity is proven by checksum in one snapshot, not by the agent saying so. Only then does it prepare the destructive SQL, with an approval ID bound to that SQL and that proof. Nothing drops until an owner approves." |
| 1:42–2:00 | neatlogs: before trace (v0) and after trace (v1) side by side | **neatlogs**: architecture (one WORKFLOW per run; AGENT, TOOL, GUARDRAIL and EVALUATOR spans), runtime (retries, lock wait, SQLSTATE on each span), result: "v0: six retries, 2.6 seconds of lock wait, paused. v1: four retries, 1.8 seconds, verified." |
| 2:00–2:15 | Terminal: `git log --oneline`, `entire checkpoint explain` on the fix commit | **Entire**: implementation and reasoning. "The naive policy was committed and measured first. This checkpoint links the fix commit to the session that produced it: the prompts, the failing numbers, the change." |
| 2:15–2:30 | cfo.ai model: base, upside, downside | **cfo.ai**: "Per-run cost comes from the agent's own cost ledger: database time and planner tokens. The model turns that into margin, break-even and runway under three scenarios." |
| 2:30–2:40 | Replay summary: 12/12, 0 false successes | "Twelve adversarial scenarios: crash after commit, parity corruption, schema drift, concurrent writes, budget cap. Zero false successes. Local fixture only; no production database yet." |

## Before recording

- Set `NEATLOGS_API_KEY` in `.env`, run `uv run parachute replay lock_timeout --planner naive` and `uv run parachute replay lock_timeout baseline_lock_timeout`, and save both trace links into `evidence/`.
- Run `uv run parachute replay lock_timeout --planner llm` (DeepSeek V4.1 Flash via OpenRouter) and show one of its decision summaries in the UI: it reads the blocker's transaction age and picks a longer wait.
- Enter the cfo.ai model from [`cfo-model-inputs.md`](cfo-model-inputs.md) and export the plan.
- Record at 1920×1080. Keep the browser zoom at 100%.
