---
description: Pick up the shared Claude/Codex handoff at session start
---

# /handoff-start — read shared session state

You are starting (or resuming) a Polymarket-bot session. Claude and Codex coordinate through a shared handoff file. **Do not act on trading files yet** — first orient.

## Step 1 — Read the handoff

1. Read `C:\tmp\session_state.md` (top-to-bottom).
2. If it does not exist, say so and stop. Ask the user how to bootstrap it.

## Step 2 — Summarize for the user

In ≤ 10 bullets, report:
- Who held it last (Claude / Codex) and when
- What is currently in flight
- Which files are owned by whom right now
- Open decision gates (paths under `C:\tmp\decision_gates\`) and their adjudicator state
- Any explicit "do not touch" lists
- The next intended action and who is supposed to take it

## Step 3 — Check current gate state

If `session_state.md` references an active gate, read that gate file and list:
- Claim count (open / resolved / blocked)
- Adjudicator state
- Anything labeled blocking that is not yet resolved by evidence

## Step 4 — Stop and wait

Do **not** edit code, deploy, run VPS commands, or update `session_state.md` yet.
Output: "Handoff loaded. Standing by for instruction." Then wait.

## Forbidden during /handoff-start
- Editing any runtime trading file (`arb_main.py`, `arb_poly_executor.py`, `poly_executor.py`, `dashboard.py`, `.env`, anything on the VPS)
- Placing orders, running signed-order smoke, unpausing bots
- Creating `/root/arb_v2_ready`, removing any `*_paused` flag
- Printing secrets, private keys, or full API tokens
- Updating `session_state.md` or any gate file
