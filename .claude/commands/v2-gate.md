---
description: Work the Polymarket V2 migration decision gate
argument-hint: [claim-id|status|audit]
---

# /v2-gate — Polymarket V2 migration gate

Deadline: **2026-04-28 11:00 UTC**. V1 clients stop working at cutover. All open orders are wiped.

## Step 1 — Read context

Always start by reading, in this order:
1. `C:\tmp\session_state.md` (handoff)
2. `C:\tmp\decision_gates\2026-04-20_migration-gate_polymarket-v2.md` (the gate)
3. `C:\tmp\v2_handoff.md` (migration spec)

## Step 2 — Check who owns the gate right now

If `session_state.md` says Codex is currently editing the gate, **stop**. Read-only mode. Report state and wait.

## Step 3 — Action depends on argument

- `audit` — list every claim, its status, and what evidence (file path / code symbol / log / on-chain tx) would resolve it. Do not edit the gate.
- `status` — short summary: open / resolved / blocked counts, adjudicator state, days to deadline.
- `<claim-id>` (e.g. `claim-10`) — focus on one claim: read it, check the cited code (e.g. `CUTOVER_TS` in `arb_main.py`), report whether evidence currently resolves, blocks, or is missing.

## Step 4 — Updates to the gate

Only edit the gate when the user has explicitly authorized it AND Codex is not the current owner per `session_state.md`. When you do edit:
- Append new evidence under the claim, with file:line references
- Do not silently flip a claim to resolved without an evidence line
- Update adjudicator state at the bottom and bump the timestamp

## Forbidden during /v2-gate
Even if a claim looks "ready":
- Do **not** place orders or run signed-order smoke
- Do **not** unpause any bot
- Do **not** create `/root/arb_v2_ready`
- Do **not** remove any pause flag — the full set is whatever the current runtime / source-map / session state lists as expected pause flags
- Do **not** print private keys, full API tokens, mnemonics, or password values
- Do **not** edit `arb_main.py`, `arb_poly_executor.py`, `poly_executor.py`, `dashboard.py`, or any `.env` unless the user names the file and explicitly authorizes the edit
- Do **not** update `session_state.md` from inside this command — that is `/handoff-end`'s job

## Step 5 — Output

End with: claim-by-claim status table, adjudicator state, and the single most useful next step. Then stop.
