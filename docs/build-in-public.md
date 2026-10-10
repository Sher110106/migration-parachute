# Build-in-public drafts

Drafts only. Nothing here has been posted. The author reviews and posts each one.

## 1. Kickoff (post now)

> Building Migration Parachute for #neatHack: an agent that runs Postgres schema migrations without taking the app down. It backfills in small chunks, backs off when another session holds the rows it needs, resumes from a saved cursor, and refuses to drop anything until a checksum proves parity and a human approves the exact SQL. Day 1: domain model, event log, Postgres adapter. @neatlogs

## 2. First failure, on purpose

> First recovery policy for #neatHack's Migration Parachute: retry the same 2,000-row batch after 50 ms. Against a row lock held by another session it burned 6 retries and 2.3 s of lock wait, then paused. It didn't corrupt anything or claim success, but it didn't finish either. Committed it as-is so the fix has an honest baseline. @neatlogs

## 3. The fix

> Fix: on a lock timeout, roll back the chunk, record the blocking session while it still holds the lock, cut the batch to 500, back off exponentially, retry from the same cursor, grow the batch back after 4 clean chunks. Same fault: 4 retries, 1.8 s lock wait, parity verified (naive: 6 retries, 2.6 s, paused). 12/12 replay scenarios. Before/after traces in @neatlogs. #neatHack

## 4. Crash test

> Killed the worker right after Postgres committed a chunk but before the agent recorded it. A new worker took the lease, read the idempotency ledger, adopted the committed chunk, and carried on. No chunk ran twice. #neatHack

## 5. Final

> Migration Parachute, built in 48 hours for #neatHack. Expand, chunked backfill, deterministic proof, owner-gated contract. Twelve adversarial replays, zero false successes, zero destructive statements without approval. Traces: @neatlogs. Build history: Entire checkpoints. Business model: cfo.ai. Demo: https://github.com/Sher110106/migration-parachute/releases/download/v0.1.0/execution-ui-replay.mp4 Repo: https://github.com/Sher110106/migration-parachute

Before posting, check each tagged handle against the official accounts.
