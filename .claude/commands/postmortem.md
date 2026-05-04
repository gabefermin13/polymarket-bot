---
description: Write a trading / bot postmortem for an incident, drawdown, or losing streak
argument-hint: [topic — e.g. "arb-2026-04-19-drawdown"]
---

# /postmortem — trading postmortem

Postmortems are blameless, evidence-driven, and stored alongside decision gates so future-Claude / future-Codex can find them.

## Step 1 — Read handoff

Read `C:\tmp\session_state.md` to confirm the incident timeframe and which bots were live.

## Step 2 — Gather evidence (read-only)

Pull (via SSH where needed):
- `positions.jsonl` rows in the incident window for the affected bot(s)
- `signals.jsonl` rows for the same window
- `bot.log` slice covering the window
- Bankroll snapshots (`shared_bankroll.json` history) before / after
- On-chain confirmation for any disputed fill / settle (Polygonscan tx hash)
- Any related decision gates

Cite everything by file path + timestamp range.

## Step 3 — Write to disk

Path: `C:\tmp\decision_gates\YYYY-MM-DD_postmortem_<topic>.md`

Structure:

```
# Postmortem: <topic>

## Window
<UTC start> → <UTC end>

## Bots involved
<list>

## What happened (timeline)
- T+0:    ...
- T+5m:   ...

## Impact
- Trades: N
- PnL: $...
- Bankroll delta: $... → $...

## Root cause
<single, falsifiable statement, citing code or data>

## Contributing factors
- ...

## What worked
- ...

## Action items
- [ ] Owner: ... — file:line — gate: ...
- [ ] Owner: ... — ...

## References
- positions.jsonl rows: ...
- signals.jsonl rows: ...
- log slice: ...
- on-chain txs: ...
```

## Step 4 — Link, don't act

If the postmortem implies a code change, file it as an action item linked to a `/strategy-gate` — do not edit the trading code in this command.

## Forbidden during /postmortem
- Do **not** edit `arb_main.py`, `arb_poly_executor.py`, `poly_executor.py`, `dashboard.py`, `.env`, or any runtime trading file
- Do **not** restart bots, unpause, or remove `*_paused` flags
- Do **not** place orders
- Do **not** print secrets
- Do **not** update `session_state.md` (use `/handoff-end`)

## Step 5 — Stop

Output: postmortem path + the top action item. Wait.
