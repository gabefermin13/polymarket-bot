---
description: Run a market-conditions ("weather") review before scaling, going live, or unpausing
---

# /weather-review — market conditions review

A "weather report" for the strategies: regime, volatility, liquidity, and recent bot performance. Use this to inform — never replace — a strategy gate.

## Step 1 — Read handoff

Read `C:\tmp\session_state.md`. Note whether any bot is live or paused; weather conclusions must be tagged with that context.

## Step 2 — Gather data (read-only)

Pull (via SSH if needed):
- BTC 1h candles, last 24–72h — compute realized vol, 4h drift, regime label
- Recent positions for each active bot (`/root/kalshiedge_*/logs*/positions.jsonl`, last 100 settled)
- Win rate by asset × direction × bot, last 50 trades
- ML model version in use (`/root/shared_ml/`) and most recent calibration timestamp
- Liquidity sample: average best ask × best bid for the assets each bot trades, last hour
- Any economic-calendar blackouts in the next 24h (NFP / CPI / FOMC / PCE)

If a bot is paused, mark its slice as "stale" rather than computing a misleading WR.

## Step 3 — Write the report

Output a short structured report:

```
## Weather review — <UTC timestamp>

### Regime
- BTC 4h drift: ...
- Realized vol: ...
- Label: Up / Down / Ranging / Chop

### Per-bot recent performance
| Bot | Status | Trades | WR | PnL | Note |

### Asset / direction flags
- e.g. ETH Down WR 33% last 30 — keep blocked
- e.g. DOGE Up WR 71% last 25 — keep allowed

### Liquidity / market structure
- ...

### Risks in the next 24h
- ...

### Recommendation
- Single sentence. Conservative bias. Reference a gate, do not act.
```

## Forbidden during /weather-review
- Do **not** change Kelly, weights, thresholds, or any `.env`
- Do **not** unpause bots or remove `*_paused` flags
- Do **not** place orders
- Do **not** restart bots
- Do **not** print secrets
- Do **not** update `session_state.md` (use `/handoff-end`)

## Step 4 — Stop

End with the recommendation and the gate that would be needed to act on it. Wait.
