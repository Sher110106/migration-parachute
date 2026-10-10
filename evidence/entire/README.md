# Entire evidence

The rules ask for "Checkpoints or entire-graph output from your repository". This folder holds both kinds of evidence that can be public. The raw session transcripts are not here; see the last section for why.

## What the recovery fix changed, from entire-graph

`entire graph commit 809753d --json` gives an entity-level change list for the fix commit against its parent, the naive-retry v0 commit `2eca418`. The Python changes:

| File | Entity | Change |
|---|---|---|
| `src/parachute/orchestrator.py` | `Orchestrator._enrich_error` | body changed: records the blocking session (pid, lock mode, transaction age) while it still holds the lock |
| `src/parachute/planners.py` | `AdaptivePlanner`, `.decide`, `.backoff`, `.__init__` | added: shrink the batch, exponential backoff with jitter seeded from the run id, grow back after clean chunks |
| `src/parachute/cli.py` | `planner_factory`, `main` | body changed: `--planner adaptive` becomes the default |

Nothing else in the agent changed. Measured on the same fixture and the same 6 s row lock, v0 pauses after 6 retries and v1 finishes after 4 (see `../replay-naive-v0.json` and `../replay-adaptive-v1.json`).

Files:

- [`graph-commit-809753d-fix.json`](graph-commit-809753d-fix.json): the fix commit
- [`graph-commit-2eca418-v0.json`](graph-commit-2eca418-v0.json): the v0 commit, which added the orchestrator, policy, verifier and replay suite
- [`checkpoints-metadata.json`](checkpoints-metadata.json): `entire checkpoint explain --json`, which lists each commit's linked checkpoint, session id, agent and date and holds no transcript bytes

To reproduce, with the Entire CLI and the `graph` plugin installed (`entire plugin install graph`):

```sh
entire graph commit 809753d --json
entire graph commit 2eca418 --json
entire graph doctor          # reports no_egress=true
git log --format='%h %s%n%(trailers:key=Entire-Checkpoint)'
```

## Checkpoints

Every commit carries an `Entire-Checkpoint` trailer. Entire's hooks did not run in the main build session, because that session started outside this repository, so its transcript was attached after each commit with `entire session attach`. That is why most commits share one checkpoint id.

## Why the transcripts are not published

Entire stores session transcripts in git refs and, by default, pushes them along with the code. The build transcript holds the author's private machine context, including a pasted project API key. Entire's secret redaction is best effort. So this repository sets `strategy_options.push_sessions: false` in `.entire/settings.json`, and the checkpoints stay on the author's machine. The rules ask for checkpoints *or* entire-graph output. They do not ask for transcripts to be public.
