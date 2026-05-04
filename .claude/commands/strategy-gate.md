---
description: Open or work a strategy decision gate before changing sizing, signals, or thresholds
argument-hint: [open <topic> | review <gate-path> | resolve <claim-id>]
---

# /strategy-gate — strategy / sizing decision gate

Any non-trivial change to strategy (new signal, weight change, Kelly fraction change, gate threshold change, new bot, new asset) goes through a falsifiable decision gate before execution.

## Step 1 — Read handoff

Read `C:\tmp\session_state.md` first. If a strategy gate is already in flight and owned by Codex, switch to read-only.

## Step 2 — Action by argument

### `open <topic>`
Create `C:\tmp\decision_gates\YYYY-MM-DD_strategy_<topic>.md` with this skeleton:

```
# Strategy gate: <topic>

## Proposal
<one paragraph>

## Why now
<motivation, expected edge, expected cost>

## Falsifiable claims
1. claim-1: <statement> — resolved_by_evidence: <what would resolve it>
2. claim-2: ...

## Counter-evidence required to block
<what observation would invalidate the proposal>

## Risk envelope
- Max bankroll exposure
- Max trades before forced re-review
- Bots affected

## Rollback plan
<how to revert>

## Adjudicator state
blocked
```

Add an entry under "Open gates" in `C:\tmp\decision_gates\index.md` if it exists.

### `review <gate-path>`
Read the gate. For each claim, identify:
- Evidence already cited (file:line, log, jsonl row, on-chain tx)
- Whether evidence currently resolves, blocks, or is missing
- The single weakest claim

Output a status table. Do not flip claim states.

### `resolve <claim-id>`
Only when the user authorizes resolution. Append the evidence line under the claim and update adjudicator state.

## Forbidden during /strategy-gate
- Do **not** deploy code to the VPS
- Do **not** restart bots, unpause, or remove `*_paused` flags
- Do **not** place orders
- Do **not** edit `arb_main.py`, `arb_poly_executor.py`, `poly_executor.py`, `dashboard.py`, `.env`, or any other runtime trading file unless the user names the file
- Do **not** print secrets
- Do **not** update `session_state.md` (use `/handoff-end`)

## Step 3 — Stop

End with the gate path and a one-line "next step." Wait.
