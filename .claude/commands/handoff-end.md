---
description: Write the shared Claude/Codex handoff at session end
---

# /handoff-end — update shared session state

You are ending (or pausing) a Polymarket-bot session. Persist the state Codex / future-Claude needs.

## Step 1 — Confirm authorization

Only run this when the user has explicitly told you to wrap up, pause, or hand off.
If the user has told you that Codex is currently working on `session_state.md`, **stop** — do not write.

## Step 2 — Read current state first

Read `C:\tmp\session_state.md` so your update is a delta, not an overwrite of unrelated sections.

## Step 3 — Update with these sections

Use append-or-replace logic. Keep the file readable and short.

```
## Last update
- Timestamp (UTC)
- Author: Claude (model id)

## What changed this session
- File-by-file list (created / edited / read-only)
- Decisions made
- Tests / smokes run and their results

## Current state of in-flight work
- What is done
- What is pending
- Known blockers

## Gate state
- Path to active gate file
- Claims resolved this session
- Claims still open / blocking

## Bots / VPS state
- Pause flags present (full set per current runtime / source-map / session state — list each expected flag and its present/absent status)
- Anything restarted / deployed (should be empty unless authorized)

## Next action
- Who picks it up (Claude / Codex / user)
- Exact next step
- Any "do not touch" list

## Forbidden carry-overs
- Confirm: no live orders placed
- Confirm: no ready flag created
- Confirm: no pause flag removed
- Confirm: no secrets printed
```

## Step 4 — Stop

Do **not** also update gate files or restart bots in the same turn unless separately authorized.
Output: a 3–5 line summary of what you wrote, plus the path. Then stop.
