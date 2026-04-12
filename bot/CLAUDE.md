# Polymarket Whale Copy-Trading Bot

## Working Style

**Only start executing when explicitly told to.** Questions, suggestions, and ideas ("how about we...", "should we...", "what if...") are for discussion — not triggers for action. Wait for an explicit go-ahead before writing scripts, deploying files, or restarting bots.

## What this is

A whale copy-trading + directional signal system for Polymarket Up-or-Down markets.

**Core insight:** Certain Polymarket wallets win 80-100% of their Up-or-Down trades over hundreds of markets. By monitoring their on-chain Polygon activity in real time and requiring N whales to agree (consensus filter), we copy only their highest-conviction signals.

**Current mode:** LIVE — real orders on Polymarket CLOB. Gone live 2026-04-12. Shared bankroll at `/root/shared_bankroll.json`. Starting bankroll $97.28; balance as of go-live: $279.20.

---

## Bot Instances (VPS: 68.183.55.155 / root / BASILSK20$$)

### C-Bots (Whale Copy-Trading)

| Bot | Dir | Consensus | Whale Pool | Log Path |
|-----|-----|-----------|------------|----------|
| C1 | `/root/kalshiedge_whalewallet_c1/` | 1 whale | 27 (curated) | `logs_c1/bot.log` |
| C2 | `/root/kalshiedge_whalewallet/` | 2 whales | ~40 (curated) | `logs/bot.log` |
| C3 | `/root/kalshiedge_whalewallet_c3/` | 3 whales | ~140 (scan-managed) | `logs_c3/bot.log` |
| C4 | `/root/kalshiedge_whalewallet_c4/` | 4 whales | ~140 (scan-managed) | `logs_c4/bot.log` |

C1/C2 whale lists are manually curated. C3/C4 are auto-managed by `scan16_auto.py`.

### D-Bots (Directional Consensus Trading) — built 2026-04-07

| Bot | Dir | Signals Required | Log Path | Status |
|-----|-----|-----------------|----------|--------|
| D1 | `/root/kalshiedge_dbot_d1/` | 1 of 7 | `logs_d1/bot.log` | **PAUSED** (commented out of watchdog) |
| D2 | `/root/kalshiedge_dbot_d2/` | 2 of 7 | `logs_d2/bot.log` | **RUNNING** — dashboard-controlled (flag: `/root/d2_paused`) |
| D3 | `/root/kalshiedge_dbot_d3/` | 3 of 7 | `logs_d3/bot.log` | **PAUSED** (commented out of watchdog) |
| D4 | `/root/kalshiedge_dbot_d4/` | 4 of 7 | `logs_d4/bot.log` | **PAUSED** (34% WR, -$232) |
| D5 | `/root/kalshiedge_dbot_d5/` | 5 of 7 | `logs_d5/bot.log` | **PAUSED** (commented out of watchdog) |
| D6 | `/root/kalshiedge_dbot_d6/` | 6 of 7 | `logs_d6/bot.log` | **PAUSED** (commented out of watchdog) |
| D7 | `/root/kalshiedge_dbot_d7/` | 7 of 7 | `logs_d7/bot.log` | **PAUSED** (commented out of watchdog) |

D2 is in watchdog DBOTS (monitored). All others commented out with `# PAUSED # ` prefix.
D2 trades: **BTC, DOGE, XRP** (SOL and ETH removed — poor WR). 5-min and 15-min Up-or-Down markets.
**D2 time-zone gates (2026-04-09 fifth session):** Dead zone (11-12 UTC) = skip entirely. Strict zone (06-08, 13, 15-17 UTC) = require n≥3.0 AND conf≥0.70. Peak zone (09-10, 14, 18-21 UTC) = Kelly×1.2. Standard (all other hours) = normal thresholds. MIN_CONFIDENCE raised 0.55→0.65.
**D4 paused 2026-04-08:** 34% WR, -$232.
**D2 start/stop:** controlled via dashboard (`POST /api/bot/d2/start` or `stop`). Flag `/root/d2_paused` prevents watchdog revival when stopped.

### Whalebot (W) — built 2026-04-08, deployed 2026-04-08

| Bot | Dir | Pool | Log Path | Status |
|-----|-----|------|----------|--------|
| W | `/root/kalshiedge_whalebot/` | 95%+ WR wallets (240+ via scan16) | `logs_w/bot.log` | **RUNNING** — dashboard-controlled (flag: `/root/w_paused`) |

- Entry script: `w_main.py`
- Whale pool: `whale_pool.json` — 240+ wallets, all 95%+ WR (expanded from 87/99%+ in fifth session), conf 0.75-0.95
- `MIN_WHALE_CONF=0.75` (raised from 0.60 — only high-confidence wallets)
- `MIN_ASK_PRICE=0.40`, `MAX_ENTRY_PRICE=0.68` — enter early like the whale, don't chase
- `MAX_CONTRACTS_PAPER=50` (raised from 25 to allow Kelly to differentiate sizes)
- scan16_auto.py writes 95%+ WR wallets to whalebot pool every 20 new wallets found (W_WR_MIN=95, W_POOL_CAP=500)
- ccc_calibrate_w.py cron (*/30) recalibrates wallet confidence from whalebot positions.jsonl
- **WalletCooldown** (replaced BiasTracker): 3 consecutive losses → 45-min cooling-off per wallet. Lighter-weight per-wallet brake vs. per-combo BiasTracker.
- **S1-S10 directional filter**: After CCC threshold check, `DirectionConsensus.evaluate()` called. If signals agree (n≥2.0 DIR_BOOST_N) → conf +0.05. If signals oppose (n≥2.0 DIR_VETO_N) → skip (`dir_signal_veto`). Entry Telegram shows `dir=±X.X`.

---

## Architecture

### C-Bot files (in each `/root/kalshiedge_whalewallet*/`)
```
whale_tracker.py      Polygon WebSocket (drpc.org) — CTF Exchange OrderFilled events
                      WHALE_ADDRESSES + WHALE_CONFIDENCE dicts
                      LOSER_ADDRESSES — actively suppressed (but don't block whale signals)
market_finder.py      Gamma-API + CLOB — maps token IDs to Up-or-Down markets
poly_executor.py      Kelly sizing, FOK paper/live orders, position tracking, P&L
main.py               Consensus filter, CCC loading, hot-reload loops, Telegram, settlement
ccc_calibrate.py      Reads all 4 positions.jsonl → calibrated wallet confidence → wallet_calibration.json
scan16_auto.py        Async wallet scanner — finds 80-100% WR wallets, auto-updates C3/C4 pool
scan_directional_fast.py  Spec A — reads existing positions.jsonl, reconstructs S1-S6 signal states per trade
```

### D-Bot files (in each `/root/kalshiedge_dbot_dN/`)
```
direction_signals.py  S1-S10 live signal functions with TTL cache (30-1800s per signal)
direction_consensus.py  DirectionConsensus class — aggregates S1-S10 into ConsensusResult; IC-weighted scoring
d_main.py             D-bot main loop — polls signals every 30s, executes on consensus
bias_tracker.py       Rolling per-(asset,direction) WR circuit breaker (D2 only) — pauses below 40% WR, resumes above 50%
poly_executor.py      Shared with C-bots (copy per dir) — patched to read MIN_CONFIDENCE/MIN_ASK_PRICE from env
market_finder.py      Shared with C-bots (copy per dir) — liquidity hard filter (MIN_LIQUIDITY_USD=500)
polymarket_data.py    Shared data layer — Gamma/Data/CLOB APIs with TTL cache
telegram_alerts.py    Shared with C-bots (copy per dir)
```

### Whalebot files (`/root/kalshiedge_whalebot/`)
```
w_main.py             Whalebot main loop — whale signal handling, settlement, Telegram, hot-reload
whale_tracker.py      Polygon WebSocket (drpc.org) — whale pool from whale_pool.json, fixed loser suppression
ccc_engine.py         CCC score computation (weighted majority, activity window)
threshold_engine.py   Dynamic threshold from S1-S6 directional signals
poly_executor.py      Kelly sizing + shared bankroll — MAX_CONTRACTS_PAPER=50, MIN_ASK_PRICE=0.40, MAX_ENTRY_PRICE=0.68
wallet_cooldown.py    Per-wallet consecutive-loss streak tracker (WalletCooldown class, in w_main.py) — 3 losses → 45-min pause
polymarket_data.py    Shared data layer — Gamma/Data/CLOB APIs with TTL cache (copy from D-bot dir)
market_finder.py      Copy from D-bot dir
direction_signals.py  Copy from D-bot dir (used by threshold_engine + directional filter gate)
direction_consensus.py  Copy from D-bot dir (used by directional filter gate in on_whale_trade)
telegram_alerts.py    Copy from D-bot dir
whale_pool.json       Hot-reload wallet pool — {addr: {name, conf, in_pool}} — 240+ wallets (95%+ WR)
wallet_calibration.json  CCC calibration output from ccc_calibrate_w.py
.env                  BOT_LABEL=W, MIN_WHALE_CONF=0.75, MIN_ASK_PRICE=0.40, MAX_ENTRY_PRICE=0.68
```

---

## Directional Signals (S1-S10)

| Signal | Source | Conf Range | IC | Weight | Status |
|--------|--------|------------|----|--------|--------|
| S1: Price Action | Coinbase 5-min candle momentum | 0.55-0.75 | ~0 | 1.0 | Built |
| S2: Order Flow | Coinbase buyer/seller imbalance | 0.55-0.78 | -0.032 | **-0.5 (contrarian)** | Built |
| S3: Market Structure | OKX funding rate (Coinglass fallback) | 0.57-0.72 | +0.05 | **1.5 (upweighted)** | Built |
| S4: Liquidation Cascades | OKX liquidation-orders ($75K threshold) | 0.60-0.82 | ~0 | 1.0 | Built |
| S5: Cross-Asset Alignment | BTC+ETH+SOL >=2/3 agree via S1 | 0.60-0.73 | ~0 | 1.0 | Built |
| S6: Polymarket Flow | data-api.polymarket.com net CLOB volume (5-min window, recency-weighted) | 0.56-0.70 | ~0 | 1.0 | Built |
| S7: Whale Consensus | Reads C-bot + W-bot signals.jsonl, last 120s | 0.75-0.95 | ~0 | 1.0 | Built |
| S8: Options Skew | Deribit ATM put-call IV skew (BTC/ETH only) | 0.57-0.72 | TBD | 1.0 | Built |
| S9: CB Premium | Coinbase vs Kraken price premium | 0.57-0.70 | TBD | 1.0 | Built |
| S10: Perp Basis | OKX USDT-SWAP vs Coinbase spot premium | 0.57-0.70 | TBD | 1.0 | Built |

**IC Analysis (2026-04-09, 814 settled trades from D1/D2/D3/D4/W):**
- IC = `weighted_fraction_correct - 0.5` with 30-day half-life time-decay
- Only S3 showed real positive IC (+0.05); S2 is contrarian (-0.032); all others ~0
- Real edge is asset/direction selection: DOGE Up 67-73% WR, ETH Down <40% WR

**IC-weighted scoring in d_main.py (deployed 2026-04-09):**
```python
SIGNAL_WEIGHTS = {
    "s1": 1.0, "s2": -0.5,  # S2 contrarian: disagreement with direction adds 0.5
    "s3": 1.5,               # S3 upweighted: counts as 1.5 votes
    "s4": 1.0, "s5": 1.0, "s6": 1.0, "s7": 1.0,
    "s8": 1.0, "s9": 1.0, "s10": 1.0,
}
```
`n_for_dir` is now a float (e.g., 3.5). Telegram entry message shows which signals fired and `n=X.X`.

**S5 logic:** fetches BTC/ETH/SOL S1 results in parallel. Signals if >=2/3 agree AND primary asset is in majority. conf=0.60 (2/3), 0.73 (3/3).

**S7 logic:** reads last ~50KB of each C-bot signals.jsonl AND W-bot `logs_w/signals.jsonl`, finds executed consensus signals for asset+direction within `S7_LOOKBACK_SECS` (default 120s). Returns conf 0.75-0.95. W-bot ts is in milliseconds; C-bots in seconds — normalized via `if raw_ts > 1e12: already ms, else multiply by 1000`. W-bot confidence field: `consensus_confidence` (added to lookup alongside `best_conf`/`confidence`/`conf`).

**Two-pass scan in d_main.py:**
1. Pass 1: evaluate S1-S5+S7 with empty token_id (S6=BALANCED) — fast filter, requires majority direction with n >= CONSENSUS_REQUIRED
2. If pass 1 passes: find market, get real token_id
3. Pass 2: re-evaluate with real token_id (S6 now live, all others cached)
4. Count signals agreeing with pass-1 direction (not requiring majority — S6 disagreeing no longer vetoes); if count >= CONSENSUS_REQUIRED → execute
5. Apply regime adjustment to final confidence before execute

**CLOB pre-entry gate in d_main.py (deployed 2026-04-08):**
- After pass-2 consensus, before execute: fetch CLOB order book for the token we're buying
- If ask_dollars / bid_dollars > CLOB_VETO_RATIO (2.0) → skip (market selling against us)
- Fallback: if CLOB unreachable → gate passes, trade proceeds
- Constant: `CLOB_VETO_RATIO=2.0` (env override)

**Regime gate in d_main.py — added 2026-04-08:**
- Fetches BTC 1h candles, computes 4h price change. Cached 900s.
- >+1.5% → regime="Up"; <-1.5% → regime="Down"; else → None (Ranging)
- With-regime trade: conf += 0.05 (max 0.95)
- Counter-regime trade: conf -= 0.05 (min 0.50)
- Ranging: no adjustment
- Constants: `REGIME_TTL=900`, `REGIME_BOOST=0.05`, `REGIME_PENALTY=0.05`

**S8: Options Skew (Deribit) — added 2026-04-09:**
- Fetches nearest-expiry ATM put IV and call IV from Deribit for BTC and ETH
- put_iv > call_iv + threshold → DOWN (market pricing in downside); call_iv > put_iv + threshold → UP
- DOGE and other assets → NEUTRAL (Deribit only lists BTC/ETH)
- `_SKEW_THRESHOLD = 1.0` IV points; TTL=300s

**S9: Coinbase Premium — added 2026-04-09:**
- Compares Coinbase REST spot price vs Kraken spot price for same asset
- CB > Kraken + threshold → UP (US demand elevated); Kraken > CB + threshold → DOWN
- `_PREMIUM_THRESHOLD = 0.0002` (0.02%); TTL=60s
- Uses Kraken (not Binance) — Binance returns HTTP 451 geo-block on VPS

**S10: Perp/Spot Basis — added 2026-04-09:**
- OKX USDT-SWAP mark price vs Coinbase spot
- Perp > spot + threshold → UP (longs paying premium = bullish); spot > perp + threshold → DOWN
- `_BASIS_THRESHOLD = 0.0001` (0.01%); TTL=60s

**Pass-2 fix (2026-04-08):** old code required consensus engine to return a majority direction in pass 2. S6=UP would tie 2v2 against S1+S5=DOWN, yielding n_signals=0 and no trade. Fix: count signals explicitly agreeing with pass-1 direction; 2 DOWN signals fire even if 2 UP signals also exist.

---

## CCC — Closed-position Confidence Calibration

Replaces static WHALE_CONFIDENCE with observed win rates from live trade data.

**Formula (Bayesian blend):**
```
n >= 10 trades:  conf = obs_wr               (full trust in observed)
n >= 5 trades:   conf = (obs_wr*n + prior*10) / (n+10)   (blend with prior)
n < 5 trades:    conf = prior                (claimed WR from scan name)
```

**Pipeline:**
1. `ccc_calibrate.py` runs every 30 min (cron) — reads all 4 `positions.jsonl` files
2. Writes `wallet_calibration.json` to all 4 bot dirs
3. Each bot's `ccc_reload_loop()` picks it up every 30 min without restart
4. `MIN_WHALE_CONF=0.60` gate — wallets below threshold excluded from consensus buffer

**Known bad wallets (conf < 0.60, auto-excluded):**
- `15151515151515` - 0.36 obs WR (-$135 total)
- `fluffyfluffyfluffy` - 0.37 obs WR (-$117 total)
- `sixx7` - 0.43 obs WR (-$67 total)
- `alwaysLastInLife` - 0.21 obs WR
- `s14_100wr_160n` - 0.38 obs WR (claimed 100%, actual 38.5%)

---

## Hot-Reload System (no restarts for wallet updates)

Two background loops in `main.py`:

**`ccc_reload_loop()`** — every 30 min
- Re-reads `wallet_calibration.json`, updates `WHALE_CONFIDENCE` dict in place

**`whale_pool_reload_loop()`** — every 15 min
- Reads `whale_pool.json`, adds new wallets to `WHALE_ADDRESSES`
- Moves evicted wallets to `LOSER_ADDRESSES`
- Also applied at startup to pick up any changes since last cold start

`scan16_auto.py` writes both `whale_tracker.py` (cold-start backup) AND `whale_pool.json` (hot-reload). **No `restart_bot()` calls** — bots pick up pool changes within 15 min.

---

## Key Environment Variables

### C-Bot `.env`
```
CONSENSUS_REQUIRED=2      # 1=C1, 2=C2, 3=C3, 4=C4
BOT_LABEL=C2
BANKROLL_USD=107
LOGS_DIR=logs             # C1=logs_c1, C2=logs, C3=logs_c3, C4=logs_c4
MAX_ENTRY_PRICE=0.82
MIN_WHALE_CONF=0.60
WHALE_MIN_SECS=30
TELEGRAM_BOT_TOKEN=8778011576:AAHOqBb2Wz8VvBZn4_5IR8BgOctSLHpI8TM
TELEGRAM_CHAT_ID=6400219232
```

### D2 `.env` (active bot — others paused)
```
CONSENSUS_REQUIRED=2
BOT_LABEL=D2
BANKROLL_USD=107
LOGS_DIR=logs_d2
SCAN_INTERVAL=30
PAPER_TRADING=1
MAX_ENTRY_PRICE=0.82
MIN_CONFIDENCE=0.65
MIN_ASK_PRICE=0.45
STRICT_MIN_CONF=0.70      # strict zone (06-08, 13, 15-17 UTC) min conf
STRICT_MIN_N=3.0          # strict zone min signals required
PEAK_KELLY_BOOST=1.2      # peak zone (09-10, 14, 18-21 UTC) Kelly multiplier
ETH_UP_MIN_CONF=0.70      # gate: ETH Up observed 33-42% WR
SOL_UP_MIN_CONF=0.85      # SOL removed from ASSETS but gate kept in env
TELEGRAM_BOT_TOKEN=8778011576:AAHOqBb2Wz8VvBZn4_5IR8BgOctSLHpI8TM
TELEGRAM_CHAT_ID=6400219232
```

### Whalebot `.env`
```
BOT_LABEL=W
BANKROLL_USD=107
LOGS_DIR=logs_w
PAPER_TRADING=1
MIN_WHALE_CONF=0.75
WHALE_CONSENSUS_WINDOW_SECS=120
THRESHOLD_BASE=0.60
THRESHOLD_MIN=0.45
THRESHOLD_MAX=0.82
THRESHOLD_STEP=0.04
CONF_BASE=0.55
CONF_MAX=0.82
MAX_KELLY_FRACTION=0.20
MAX_ENTRY_PRICE=0.68
MIN_ASK_PRICE=0.40
MIN_CONFIDENCE=0.55
WHALE_MIN_SECS=30
TELEGRAM_BOT_TOKEN=8778011576:AAHOqBb2Wz8VvBZn4_5IR8BgOctSLHpI8TM
TELEGRAM_CHAT_ID=6400219232
```

**NOTE:** `MIN_CONFIDENCE=0.95` was removed from all C-bot `.env` files on 2026-04-07.
**NOTE:** D-bot `poly_executor.py` copies are patched so `MIN_CONFIDENCE` and `MIN_ASK_PRICE` read from env. C-bot copies are NOT patched (use hardcoded 0.65 / 0.70).
**NOTE:** `MIN_15M_CONF` gate removed 2026-04-07 — 15-min markets showed 52.3% WR, were being incorrectly blocked.
**NOTE:** `SOL_UP_MIN_CONF` raised to 0.85 on 2026-04-08. SOL also removed from ASSETS in D2 d_main.py.
**NOTE:** D-bot `poly_executor.py` requires `import fcntl` — was missing from deployed copies, causing silent settlement failures 2026-04-08 00:01-00:33 UTC. Fixed via `sed -i '/^import os$/i import fcntl'`.
**NOTE:** `MAX_CONTRACTS_PAPER` raised to 50 on D2 and Whalebot poly_executor.py — allows Kelly to differentiate trade sizes instead of always hitting the 25-contract cap.
**NOTE:** D2 `ASSETS = ["BTC", "ETH", "DOGE"]` — SOL removed, DOGE added 2026-04-08.
**NOTE:** Regime gate (S8) bug fixed 2026-04-08 — original deploy referenced `r2.conf` before `r2` was assigned (UnboundLocalError on every scan, zero trades). Fixed by moving regime block to after pass-2 evaluation.
**NOTE:** Pass-2 consensus fix 2026-04-08 — old majority-check caused 2v2 ties (S6 opposing) to yield n_signals=0. Fixed to count signals agreeing with pass-1 direction explicitly.
**NOTE:** W-bot polymarket_data.py + market_finder.py deployed 2026-04-09 — liquidity gate (MIN_LIQUIDITY_USD=500) and CLOB gate (CLOB_VETO_RATIO=2.0) added to `on_whale_trade()`. Constants env-overridable. Shutdown: `await polymarket_data.close()`.
**NOTE:** BiasTracker integrated into W-bot 2026-04-09 — `bias_tracker.py` wired into `on_whale_trade()` (gate: skip if paused) and `settlement_loop()` (record: `bias_tracker.record(asset, direction, won)` inside `if whale_name:` block). Warmed from `positions.jsonl` on startup. `MIN_WINDOW=3`, `PAUSE_THRESHOLD=0.40`, `RESUME_THRESHOLD=0.50`, `WINDOW_SIZE=15`.
**NOTE:** Curated wallets removed from W-bot `whale_tracker.py` 2026-04-09 — jaicobioas, 2ez4mee, MILKinDenial, 111111116 removed (conf=0.00 bug from whale_pool_reload_loop overwriting them). Pool: 91 → 87 wallets. If genuinely 99%+ WR, scan16 will rediscover them.
**NOTE:** Watchdog SyntaxError fixed 2026-04-09 — commented-out whalebot command in SCRIPTS tuple left unclosed `(`, causing `SyntaxError: closing parenthesis ']' does not match opening parenthesis '('` on every cron tick. Watchdog was completely dead. Fixed by restoring command string.
**NOTE:** Dashboard start/stop API added 2026-04-09 — `POST /api/bot/{d2|w}/start` (remove flag + spawn) and `/stop` (create flag + kill). `POST /api/reset` clears positions.jsonl + resets bankroll to $97.28. Flag files `/root/d2_paused` and `/root/w_paused` prevent watchdog revival when stopped.
**NOTE:** BiasTracker added to D2 2026-04-09 — `bias_tracker.py` copied from whalebot, integrated into `d_main.py` with warmup, gate (after choppiness), and record (after quality_model). ETH Down suppression now dynamic instead of hardcoded.
**NOTE:** CLOB_VETO_RATIO raised to 20.0 on both D2 and W (2026-04-09) — default of 2.0 was blocking all trades; Polymarket CLOB naturally has ask:bid ~10-15x due to market maker structure. 20.0 only vetoes genuinely extreme imbalance.
**NOTE:** Dashboard open positions table — "Time Open" replaced with "Expires In" (countdown to market end_time). Shows `Xm Ys` or `expired`.
**NOTE:** MAX_MARKET_SECS_LEFT=1800 added to W-bot w_main.py (2026-04-09) — skips markets with >30 min remaining. Prevents capital being tied up for hours in long-duration markets. Env-overridable. Skip reason: `market_too_far_out`.
**NOTE:** ETH_UP_MIN_CONF gate bug fixed 2026-04-09 (third session) — `ETH_UP_MIN_CONF=0.70` was in D2 `.env` but never read by `d_main.py`. All 4 ETH Up trades in early test fired at conf=0.63-0.638 (below threshold). Fixed by adding `_combo_min_conf(asset, direction_pm)` helper that reads `{ASSET}_{DIRECTION}_MIN_CONF` from env. Gate inserted after bias_tracker check. Pattern is generic — any combo gate (e.g. `BTC_DOWN_MIN_CONF`) works automatically.
**NOTE:** Dashboard open positions `end_time` filter added 2026-04-09 (third session) — `/api/open_positions` now filters orphaned/expired positions with `end_time > time.time()` before returning results. Previously, positions from before a bankroll reset or bot restart appeared as permanently open.
**NOTE:** Whalebot S6 (Polymarket CLOB flow) confirmed active — used in two ways: (1) ThresholdEngine calls S6 with `signal.token_id` (whale's actual position token) to adjust CCC threshold ±0.04 per signal; (2) CLOB bid/ask veto gate (CLOB_VETO_RATIO=20.0) hard-blocks extreme imbalance. Both live.
**NOTE:** D2 time-zone gate added 2026-04-09 (fifth session) — `_DEAD_HOURS={11,12}` skip entirely; `_STRICT_HOURS={6,7,8,13,15,16,17}` require n≥3.0 AND conf≥0.70; `_PEAK_HOURS={9,10,14,18,19,20,21}` apply Kelly×1.2 boost. `BASE_MIN_CONF` raised 0.55→0.65. Zone shown in Telegram entry (`zone=peak/strict/standard`).
**NOTE:** D2 ASSETS expanded to include XRP 2026-04-09 (fifth session) — `ASSETS = ["BTC", "ETH", "DOGE", "XRP"]`. Added `"XRP": "XXRPZUSD"` to `_KRAKEN_PAIRS` in `direction_signals.py` (S9 Coinbase premium). S8 NEUTRAL for XRP (Deribit no XRP options). All other signals work.
**NOTE:** D2 ETH removed from ASSETS 2026-04-11 (eleventh session) — 130-trade analysis showed ETH Up 33% WR, ETH Down 31% WR across both bots. Now `ASSETS = ["BTC", "DOGE", "XRP"]`. ETH had zero positive alpha.
**NOTE:** W-bot combo blocks added 2026-04-11 (eleventh session) — `_BLOCKED_COMBOS = {("ETH","Up"),("ETH","Down"),("BTC","Up")}` inserted in `on_whale_trade()` after `asset = signal.symbol.split("-")[0]`. ETH both dirs: 31-33% WR across 25 trades. BTC Up: 30% WR across 20 trades. Logged as `combo_blocked`.
**NOTE:** Combo-tier Kelly multiplier added to D2 and W 2026-04-11 (eleventh session) — data-driven sizing by observed WR tier. `_COMBO_KELLY_TIERS = {("BTC","Down"):1.3, ("XRP","Down"):1.0, ("DOGE","Up"):1.0, ("XRP","Up"):1.0}`, default=0.8. All env-overridable via `TIER_KELLY_BTC_DOWN`, `TIER_KELLY_DEFAULT`, etc. Tier multiplier sits inside the Kelly chain (after ML kelly_scale, before Kronos/EV multipliers); poly_executor still applies MAX_KELLY_FRACTION cap and correlation discount on top.
**NOTE:** D2 ML_MIN_BTC_UP raised 0.85→0.99 2026-04-11 (eleventh session) — effectively blocks BTC Up on D2 (no ML model returns p≥0.99). Cleaner than removing from ASSETS (preserves signal logging).
**NOTE:** Dashboard balance chart fixed 2026-04-11 (eleventh session) — `_balance_history()` rewritten: uses close events only, delta=realized_pnl (not cost+pnl), starts from `initial_balance` in bankroll.json (not hardcoded 107). Chart ends at ~$163 = $97.28 + $65.86 realized P&L (accurate per positions.jsonl). bankroll.json ($128.84) is stale — only updated by force_settle, not normal bot ops.
**NOTE:** Dashboard three-way balance breakdown added 2026-04-11 (eleventh session) — `_compute_balances()`: total = initial + realized_pnl; deployed = sum of open position costs; liquid = total − deployed. Hero now shows 5 cells: Total Balance, Liquid Cash, Deployed, P&L, Win Rate. CSS: `grid-template-columns: repeat(5, 1fr)`.
**NOTE:** Dashboard chart timezone changed to America/Los_Angeles 2026-04-11 (eleventh session).
**NOTE:** Dashboard restart requires uvicorn — `pkill -f "uvicorn dashboard"` then `uvicorn dashboard:app --host 0.0.0.0 --port 8080`. Not `python3 dashboard.py`.
**NOTE:** 130-trade analysis 2026-04-11 (eleventh session) — BTC Down 77% WR +$69.73 is carrying all profits. ETH Up/Down and BTC Up are systematically negative. W:L ratio 1.14x (structural: avg entry ~$0.50 gives natural 1.0x ratio). Path to 1.3-1.5x W:L: combo cleanup first, then ML/EV quality improvement.
**NOTE:** ML backfill expanded to 164,094 markets 2026-04-11 (eleventh session) — `training_data.jsonl` complete, 0 errors. Ready for retraining model v6 (v5 was trained on 95K markets).
**NOTE:** Gone live 2026-04-12 (twelfth session) — PAPER_TRADING=0 set in both D2 and W .env. Polymarket credentials added: POLYMARKET_PRIVATE_KEY + POLYMARKET_ADDRESS (proxy wallet). ClobClient initialized with `signature_type=1, funder=ADDR` for Magic.link proxy wallet architecture. API key derived: `3f87d8e5-3d99-feea-72e2-ed4a381b6027`. Connection verified via `create_or_derive_api_creds()` + `get_orders()` authenticated access.
**NOTE:** Polymarket proxy wallet architecture 2026-04-12 — Magic.link key derives to EOA address (different from deposit address). Deposit address is a CREATE2 proxy wallet. `py_clob_client` requires `signature_type=1` (POLY_PROXY) and `funder=PROXY_WALLET_ADDRESS`. Without these, orders fail silently. Both poly_executor.py copies updated.
**NOTE:** econ_calendar.py ISO parser fix 2026-04-12 (twelfth session) — `_parse_event_ts()` rewrote to handle ISO datetime (`"2026-04-14T08:30:00-04:00"`) from cache file. Prior version expected separate date/time fields and silently failed open (no blackouts fired). Both D2 and W copies fixed.
**NOTE:** Watchdog race condition fix 2026-04-12 (twelfth session) — dashboard `api_stop` added `await asyncio.sleep(1)` between `BOT_PAUSE_FLAGS[bot].touch()` and `_kill_bot()`. Without this, watchdog cron could fire at the same second the bot was killed and see the flag not yet created — reviving the bot immediately. Fix: write flag, wait 1s, then kill.
**NOTE:** W .env misconfiguration fixed 2026-04-12 — was written with THRESHOLD_BASE=0.10, MIN_ASK_PRICE=0.05, etc. (corrupted values). W ran with bad config 07:55-09:22 UTC April 12. Restored to correct values from CLAUDE.md.
**NOTE:** BiasTracker re-enabled as hard block 2026-04-12 — Step 3 (demote BiasTracker to logging-only) was partially implemented but reverted after 7 consecutive losses on BTC Down. BiasTracker remains a hard block gate. Revisit demotion only after AutocorrTracker accumulates ≥5 trades per combo (WINDOW=5).
**NOTE:** Per-asset open position cap added to D2 d_main.py 2026-04-12 — `MAX_XRP_POSITIONS=2`, `MAX_ASSET_POSITIONS=3` env vars. Gate inserted after combo_min_conf check. Skip reason: `asset_open_cap`.
**NOTE:** 30¢ YES→Down flip added to D2 d_main.py 2026-04-12 (Step 2) — if direction="Up" and `_ev.ask < 0.30`, flip to "Down" market and re-run EV. Only executes if a Down market exists.
**NOTE:** MarketOrderArgs missing `side` arg fixed 2026-04-12 — `poly_executor.py` called `MarketOrderArgs(token_id, amount, price, order_type)` but the signature requires `side` as the 3rd positional arg. Every live order since go-live failed silently with `live_execution_failed`. Fixed: `side="BUY"` added. Both D2 and W poly_executor.py patched.
**NOTE:** ML per-combo boost+floor override added to W w_main.py 2026-04-12 — after `_ml.should_trade()` fails, checks `ML_BOOST_{ASSET}_{DIR}` (additive boost to p_win) and `ML_MIN_{ASSET}_{DIR}` (lowered floor). `ML_BOOST_BTC_DOWN=0.15` + `ML_MIN_BTC_DOWN=0.55` in W .env — BTC Down (77% WR best combo) was being blocked at p=0.41 by ML v5 threshold of 0.70. Boosted: 0.41+0.15=0.56 >= 0.55 → passes. Not a full bypass — still requires boosted p_win to clear the lowered floor.
**NOTE:** MAX_MARKET_SECS_LEFT=900 confirmed working in D2 2026-04-11 (eleventh session) — gate at line 7744, 12-space indent inside `_scan_once()` try block. `continue` correctly skips to next `for asset in ASSETS:` iteration. All log violations were from before investigation window.
**NOTE:** W-bot BiasTracker replaced with WalletCooldown 2026-04-09 (fifth session) — BiasTracker was blocking 68% of all W signals (per-combo; if DOGE Up had a bad run, ALL DOGE Up blocked). Replaced with per-wallet streak: 3 consecutive losses → 45-min cooldown for that wallet. Much lighter gate.
**NOTE:** W-bot S1-S10 directional filter added 2026-04-09 (fifth session) — After CCC threshold check, calls `DirectionConsensus.evaluate()`. If signals agree with whale direction and n≥DIR_BOOST_N (2.0) → conf +0.05. If signals oppose and n≥DIR_VETO_N (2.0) → veto (`dir_signal_veto`). Cached via ThresholdEngine (no duplicate API calls). Entry Telegram shows `dir=+X.X` or `dir=-X.X`.
**NOTE:** W-bot whale pool expanded 95%+ WR 2026-04-09 (fifth session) — `W_WR_MIN` lowered 99→95, cap removed (`W_POOL_CAP=500`). scan16 found 374 wallets 95%+ WR; 240+ written to whale_pool.json. W pool updated every 20 new wallets (not just at C3 trigger).
**NOTE:** Force-settle pattern for stuck positions — use `httpx.Client` (not urllib — gets 403) to fetch `clob.polymarket.com/last-trade-price?token_id=FULL_TOKEN`. Only settle if price ≤0.15 or ≥0.85. Write close events directly to positions.jsonl; credit payout via `fcntl` locking to shared_bankroll.json. Used 2026-04-09 (fifth session) to recover $28.54 from 6 expired positions.
**NOTE:** Polygon WebSocket stale detection — if `poly_events` counter in DIAG frozen for 30+ min, WebSocket is connected but dead (drpc.org silent). Fix: restart W-bot for fresh connection. Occurred 2026-04-09 fifth session; `poly_events=203212` frozen 65+ min.
**NOTE:** S9 (Coinbase premium) XRP pair fix — added `"XRP": "XXRPZUSD"` to `_KRAKEN_PAIRS` in `direction_signals.py`. Without this, S9 returned NEUTRAL for XRP (KeyError caught silently).
**NOTE:** scan16_auto.py cron changed to `0 */6 * * *` (every 6 hours) — was run-on-demand / `*/30`; now scheduled so W pool stays fresh.
**NOTE:** drift_ev.py deployed 2026-04-10 (sixth session) — shared module in both D2 and W dirs. Implements Brownian bridge EV gate: P(win)=Φ(drift/σ(τ)), EV=P(win)×0.99−ask, threshold 0.04. Reference price = Coinbase candle OPEN at market resolution window start. All constants env-overridable.
**NOTE:** D2 EV gate (sixth session) — inserted after CLOB gate, before execute. Logs `low_ev` to signals.jsonl if EV<0.04. Boosts adj_conf by ev×0.5 when passing. Blocks ~15% of signals in early testing.
**NOTE:** W-bot EV gate + MIN_WHALE_ENTRY (sixth session) — whale entry price filter (skip if signal.price < 0.52) added first. Then compute_ev() followed by wait_for_ev_window() polling up to 90s. Entry analysis: whales entering ≥0.65 had 100% WR vs 0% WR at ≤0.55 — confirms drift/momentum is the real signal.
**NOTE:** W-bot signals.jsonl "unknown" skip reasons — 392 of 500 analyzed have no skip_reason field. These are records written before gates were applied (CCC threshold check, market find failures). Not a bug; they represent signals that passed CCC threshold but were blocked before skip_reason was set in the log record.
**NOTE:** Frankfurt Binance proxy deployed 2026-04-10 (seventh session) — DigitalOcean Frankfurt droplet (138.197.181.139, $4/mo). SSH: `key_filename=~/.ssh/id_ed25519`. Proxy at port 8081, token `poly_binance_proxy_2026`. Runs as systemd service (`binance-proxy.service`). venv at `/root/proxyenv/`. Unlocks Binance API (CVD, OI, aggTrades, klines) for NYC bot.
**NOTE:** 36GB Polymarket dataset downloaded 2026-04-10 (seventh session) — from `https://s3.jbecker.dev/data.tar.zst`. Extracted to `C:\tmp\data\`. 408,863 Polymarket markets total; 91,148 resolved crypto Up-or-Down (BTC/ETH/XRP/DOGE). Labels from `outcome_prices` field — no resolution methodology reverse-engineering needed. Markets parquet at `C:\tmp\ml_data\markets_updown.parquet`.
**NOTE:** ML pipeline architecture 2026-04-10 (seventh session) — XGBoost + Platt scaling calibration replaces sequential gate stack. Kelly scaling: p_win at threshold=0.5×, 0.60=1.0×, 0.70+=1.5×. Hot-reload every 30 min. Graceful fallback if model not loaded. All files in `C:\tmp\`: ml_features.py, ml_filter_dataset.py, ml_backfill.py, ml_train.py, ml_predict.py, ml_calibrate.py. Deployed to `/root/shared_ml/` on NYC VPS.
**NOTE:** ml_filter_dataset.py trade filtering fixed 2026-04-10 (eighth session) — original filter matched condition_ids against token_ids (never matched, 0 results). Fixed to extract token IDs from `clob_token_ids` column, filter trades by token ID, compute ask_price from earliest trade per market (block_number+log_index sort, timestamp=null), join ask_price back to markets_updown.parquet. Resume logic pre-filters args_list in main process (skips files with existing chunks). ~27.9M matching rows from high-trade-ID files (100M+ range). Low-ID files (<100M) are pre-crypto-market trades with 0 matches.
**NOTE:** d_main.py + w_main.py ML gate integrated 2026-04-10 (eighth session) — `ml_predict.py` wired into both bots. Gate after EV pass, before execute. No model = passthrough (kelly_scale=1.0, always trades). Model loaded = p_win gate + kelly scaling. Local files: `C:\tmp\d_main_latest.py`, `C:\tmp\w_main.py`. Deployed 2026-04-11 (ninth session).
**NOTE:** ML model v5 trained and deployed 2026-04-11 (ninth session) — CV AUC 0.774 (up from 0.615 with wrong timestamps). Key fixes: (1) features computed at `window_start_ts = end_ts - duration_secs` (resolution window start, not market creation time); (2) added bn_momentum_15m/30m/60m, realized_vol_30m, price_vs_ma20/ma60; (3) symbol×direction combo features; (4) dropped zero-filled columns (s9_premium, s10_basis, bn_cvd_1m); (5) parallel backfill with 8 workers (9.6 min vs 35 min). Holdout WR (last 30 days): 90.9% at p≥0.70. Temporal stability: first half 89.4% WR, second half 86.0% WR. Deployed threshold: p≥0.70. Model at `/root/shared_ml/`.
**NOTE:** ML retraining cron ml_calibrate.py (ninth session) — reads settled trades from D2+W positions.jsonl, extracts features from signals.jsonl, combines with historical data (rolling 90-day window), retrains XGBoost every 30 min. Cron: `*/30 * * * * python3 /root/ml_calibrate.py >> /tmp/ml_calibrate.log 2>&1`. Rolling window prevents stale regime data diluting model.
**NOTE:** ML Kelly stacking with whale signal (ninth session, coworker recommendation) — when both ML p≥0.70 AND whale consensus fire → max Kelly (1.5×). ML alone (D2, no whale) → full Kelly. W-bot: whale+ML → full Kelly, whale alone → normal Kelly (no ML scaling), ML veto → skip. Deploy threshold p≥0.70 (holdout WR 83.4% on full holdout, 90.9% on last 30 days). Monitor live WR on first 200 trades; if holds 80%+, Kelly fully open.
**NOTE:** S11 CVD signal added (tenth session) — `s11_cvd()` in direction_signals.py. Binance aggTrades via Frankfurt proxy (138.197.181.139:8081), 5-min window, net buy fraction threshold 0.08. Returns UP/DOWN/NEUTRAL, conf 0.55-0.75. Runs in parallel with S1-S6 in direction_consensus.py. Weight=1.0 in SIGNAL_WEIGHTS. TTL=60s. Also deployed direction_signals.py + direction_consensus.py to whalebot dir.
**NOTE:** Late-entry scanner added to D2 (tenth session) — separate async loop in d_main.py, scans every 15s for markets with 20-120s remaining. Pure Brownian bridge EV (no signal consensus). Threshold EV≥0.10, half-Kelly (0.5×), max 3 concurrent late positions. Logs as source=late_entry. Does not apply ML gate. Runs alongside signal_scan_loop in asyncio.gather.
**NOTE:** CoinbaseWS deployed (tenth session) — coinbase_ws.py, persistent WebSocket to `wss://ws-feed.exchange.coinbase.com`, subscribes to ticker for BTC/ETH/DOGE/XRP-USD. Prices updated every tick (~100ms). drift_ev._get_curr_price() reads WebSocket first (eliminates 6-10s REST latency). Auto-reconnects. Module-level singleton via set_instance()/get_instance(). Deployed to D2 and whalebot dirs.
**NOTE:** Economic calendar gate added (tenth session) — econ_calendar.py, fetches Forex Factory weekly JSON (https://nfs.faireconomy.media/ff_calendar_thisweek.json), filters High-impact USD events. Blackout window: 10 min before → 5 min after each release. Blocks entire scan cycle (not per-asset). Fails open (no block if calendar unavailable). Cache TTL=4h. 8 events loaded on first run. Deployed to D2 and whalebot dirs.
**NOTE:** AutocorrTracker added to D2 (tenth session) — tracks last 3 outcomes per (asset, direction). 2/3+ wins → +0.04 conf boost. 0/3 or 1/3 wins → -0.04 conf penalty. Warmed from positions.jsonl on startup. Record called in settlement_loop alongside BiasTracker. Distinct from BiasTracker (which blocks on streaks; autocorr applies gradient adjustment).
**NOTE:** drift_ev.py resolution tightening (tenth session) — (1) market_start_ts now minute-aligned: `int(ts/60)*60` in both compute_ev() and _get_ref_price(). (2) Binance 1-min kline via Frankfurt proxy added as fallback for ref_price when Coinbase fails. (3) Binance ticker via proxy added as fallback for curr_price. Priority: WebSocket → Coinbase REST → Binance proxy.
**NOTE:** poly_executor.py Kelly correlation adjustment (tenth session) — `_corr_discount()` method computes Kelly multiplier based on open correlated positions. Correlation matrix: BTC-ETH=0.80, BTC-XRP=0.65, BTC-DOGE=0.60, ETH-XRP=0.60, ETH-DOGE=0.55, DOGE-XRP=0.55. Same direction: discount = 1-(corr×0.5) per open position. Opposite direction: 1-(corr×0.1). Floor=0.25. Also added `kelly_multiplier` param to execute() and `(pos, skip_reason)` tuple return. Deployed to D2 and whalebot.
**NOTE:** GitHub token stored for pushes — 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 (remote set on C:\tmp git repo). Push: `git push origin master`.
**NOTE:** Go-live plan (tenth session) — after 24-48h paper run with full stack (S11+late-entry+WebSocket+econ+autocorr+corr-Kelly), flip PAPER_TRADING=0 in D2 and W .env files and restart. All live order infrastructure already built in poly_executor.py.

---

## Blockchain Details

- **Network:** Polygon (chain ID 137)
- **CTF Exchange:** `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e`
- **Event topic:** `0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6` (OrderFilled)
- **WebSocket RPC:** `wss://polygon.drpc.org` - publicnode.com silently broke log streaming
- **Buy detection:** whale gives USDC (asset ID = 0) = buying position token
- **Sell detection:** whale gives position token = exiting (ignored, not copied)
- **Loser suppression:** only suppresses if loser is on one side AND no whale on the other

---

## Risk Controls (poly_executor.py)

```python
MAX_KELLY_FRACTION   = 0.20   # cap at 20% bankroll per trade
MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE", "0.65"))  # env-override in D-bots and W
MAX_CONTRACTS_PAPER  = 25     # D2 and W raised to 50 — allows Kelly differentiation
MAX_CONTRACTS_LIVE   = 15
MIN_MARKET_SECS_LEFT = 28
MAX_OPEN_POSITIONS   = 10
MIN_TRADE_DOLLARS    = 1.00
MIN_ASK_PRICE        = float(os.getenv("MIN_ASK_PRICE", "0.70"))   # env-override: D2=0.45, W=0.40
MAX_ENTRY_PRICE      = float(os.getenv("MAX_ENTRY_PRICE", "1.0"))  # W=0.68 (enter early, don't chase)
TAKER_FEE            = 0.01
```

**C-bot `poly_executor.py`:** unchanged hardcoded values (0.65 / 0.70), MAX_CONTRACTS_PAPER=25.
**D2 `poly_executor.py`:** patched to read from env — MIN_CONFIDENCE=0.55, MIN_ASK_PRICE=0.45, MAX_CONTRACTS_PAPER=50.
**W `poly_executor.py`:** same patches — MIN_CONFIDENCE=0.55, MIN_ASK_PRICE=0.40, MAX_ENTRY_PRICE=0.68, MAX_CONTRACTS_PAPER=50.

---

## Wallet Scanning (scan16_auto.py)

Async scanner — scrapes Polymarket activity, scores wallets by Up-or-Down WR.

**Win/loss detection (fixed 2026-04-05):**
- WIN: `TRADE side=SELL price >= 0.90` OR `REDEEM usdcSize > 0`
- LOSS: has exit (SELL or REDEEM) but none profitable
- SKIP: open position (BUY only, no exit)

**Pool targets:** C3 = 99%+ WR wallets, C4 = 80%+ WR wallets, W = 95%+ WR wallets, no cap (W_WR_MIN=95, W_POOL_CAP=500)

**Graph expansion:** qualifiers' conditionIds feed next round to find co-traders

**Auto-update:** when targets met, writes `whale_tracker.py` + `whale_pool.json` to bot dirs (no restart needed)

**Whalebot pool update (2026-04-09 fifth session):** `W_WR_MIN` lowered 99→95, `W_POOL_CAP=500` (effectively uncapped). W pool written every 20 new 95%+ wallets found during main scan (not just at C3 trigger). Scan found 374 total 95%+ wallets in 11 rounds → 240+ in whale_pool.json. Conf formula: `min(0.95, max(0.75, (wr/100)*0.95))`. Format: `{addr: {"name": str, "conf": float, "in_pool": true}}`.

**Cron:** scan16 changed to `0 */6 * * *` (every 6 hours, was `*/30`).

---

## Cron Jobs

```
*/5  * * * *  python3 /root/watchdog.py >> /tmp/watchdog.log 2>&1
*/30 * * * *  /root/polybot_backup.sh >> /tmp/backup.log 2>&1
*/30 * * * *  python3 /root/kalshiedge_whalewallet/ccc_calibrate.py >> /tmp/ccc_calibrate.log 2>&1
*/30 * * * *  python3 /root/dbot_calibrate.py >> /tmp/dbot_calibrate.log 2>&1
*/30 * * * *  python3 /root/ccc_calibrate_w.py >> /tmp/ccc_calibrate_w.log 2>&1
0    */6 * * *  python3 /root/kalshiedge_whalewallet/scan16_auto.py >> /tmp/scan16_auto.log 2>&1
```

**Note:** watchdog.py monitors C1-C4 (paused via `/root/cbots_paused`), D2 only (D1/D3-D7 commented out), W-bot, scan16, and dashboard. dbot_calibrate.py writes `/root/signal_calibration.json`; D-bots hot-reload it every 30 min. ccc_calibrate_w.py writes wallet_calibration.json to whalebot dir.
**Note:** watchdog had a SyntaxError (commented-out command left unclosed paren in SCRIPTS tuple) — was completely dead until fixed 2026-04-09.

---

## Logs

| File | Contents |
|------|----------|
| `bot.log` | All stdout from bot process |
| `logs/signals.jsonl` | Every signal (executed + skipped) with full signal breakdown |
| `logs/positions.jsonl` | Every trade open/close with P&L |

**D-bot positions.jsonl fields:** `id`, `condition_id`, `token_id`, `direction`, `symbol`, `title`, `source`, `confidence`, `contracts`, `entry_price`, `cost_usd`, `end_time`, `ts_open`, `ts_close`, `exit_price`, `realized_pnl`, `status` (open/won/lost), `paper`, `event` (open/close)

---

## Telegram Alert Formats

### C-bots
- Entry: `[C2] BTC-USD Up | conf=0.91 @0.510 $12.88`
- No settlement alerts (logs only)

### D-bots
- Entry: `[D3] SOL Up | n=3 conf=0.712 @0.510 $12.88 [4m30s]`
- Settlement WIN: `[D3] ✅ SOL-USD Up WON +$13.00 (0.510→0.990)\nSession: W4/L1 80% PnL=$+24.50`
- Settlement LOSS: `[D3] ❌ BTC-USD Up LOST -$12.88 (0.510→0.000)\nSession: W4/L2 67% PnL=$+11.62`

**Note:** Positions entered before a bot restart are orphaned (not in memory) and will NOT generate settlement alerts. Only positions entered after the latest restart settle via Telegram. Session P&L resets on each restart.

### D2 (updated 2026-04-09 fifth session)
- Entry: `[D2] BTC Down | n=2.0 conf=0.693 zone=peak @0.510 $18.32 [4m30s]\nS1 S3 S5`
- Settlement WIN: `[D2] ✅ BTC-USD Down WON +$19.10 (0.510→0.990)\nBankroll: $85.20→$104.30\nSession: W4/L1 80% PnL=$+24.50`
- Settlement LOSS: `[D2] ❌ ETH-USD Down LOST -$18.32 (0.510→0.000)\nBankroll: $104.30→$85.98\nSession: W4/L2 67% PnL=$+6.18`

### Whalebot (updated 2026-04-09 fifth session)
- Entry: `[W] BTC Up | ccc=0.81 thr=0.54 n=4 dir=+2.5 @0.510 $18.32`
- Settlement WIN: `[W] ✅ BTC-USD Up WON +$9.31 (0.500→0.990)\nBankroll: $41.20→$50.51`
- Settlement LOSS: `[W] ❌ ETH-USD Down LOST -$18.32 (0.510→0.000)\nBankroll: $50.51→$32.19`

---

## Operational Notes

- SSH via paramiko: `allow_agent=False, look_for_keys=False, password='BASILSK20$$'`
- **SSH rate limit:** Opening many rapid connections triggers sshd `MaxStartups`. Use a single persistent SSH connection, or wait 30-60s if blocked.
- `pip install` requires `--break-system-packages` (Debian externally-managed)
- Watchdog runs every 5 min — detects and kills duplicates, revives dead bots
- **To pause C-bots without stopping watchdog:** `touch /root/cbots_paused` — watchdog skips C-bot revival while this file exists. `rm /root/cbots_paused` to re-enable. Currently: **C-bots paused.**
- **To pause/resume D2 or W via dashboard:** `POST /api/bot/{d2|w}/stop` (kills + creates flag) or `POST /api/bot/{d2|w}/start` (removes flag + spawns). Watchdog respects flags and won't revive.
- **To pause D2/W without dashboard:** `touch /root/d2_paused` or `touch /root/w_paused` — watchdog won't revive. `rm` to re-enable.
- **To pause other D-bots:** comment out line in `/root/watchdog.py` with `# PAUSED # ` prefix. Currently: D1, D3, D4, D5, D6, D7 paused.
- **Reset trade data + bankroll:** `POST /api/reset` — clears all positions.jsonl, resets bankroll to $97.28.
- **To kill D2 cleanly (manual):** `pkill -9 -f "python3 d_main.py"`
- **To kill whalebot cleanly (manual):** `pkill -9 -f "python3 w_main.py"`
- Always use `nohup` for scans: `nohup python3 scan16_auto.py > /tmp/scan16_out.txt 2>&1 &`
- Deploy files via paramiko SFTP, then restart bots manually

**Kill a C-bot (CWD-based):**
```python
import os
bot_dir = '/root/kalshiedge_whalewallet_c1'
for pid in os.listdir('/proc'):
    if not pid.isdigit(): continue
    try:
        if os.readlink('/proc/'+pid+'/cwd') == bot_dir:
            raw = open('/proc/'+pid+'/cmdline','rb').read().split(b'\x00')
            if raw and b'python' in raw[0] and len(raw)>1 and raw[1].strip(b'/') == b'main.py':
                os.kill(int(pid), 15)
    except: pass
```

**Kill a D-bot:**
```python
import os
bot_dir = '/root/kalshiedge_dbot_d3'
for pid in os.listdir('/proc'):
    if not pid.isdigit(): continue
    try:
        if os.readlink('/proc/'+pid+'/cwd') == bot_dir:
            raw = open('/proc/'+pid+'/cmdline','rb').read().split(b'\x00')
            if raw and b'python' in raw[0] and len(raw)>1 and b'd_main' in raw[1]:
                os.kill(int(pid), 15)
    except: pass
```

**Start a C-bot:**
```bash
cd /root/kalshiedge_whalewallet_c1
set -a && source .env && set +a
nohup python3 main.py >> logs_c1/bot.log 2>&1 &
```

**Start a D-bot:**
```bash
cd /root/kalshiedge_dbot_d3
set -a && source .env && set +a
nohup python3 d_main.py >> logs_d3/bot.log 2>&1 &
```

---

## Known Issues / TODOs

- **Telegram `/status` reads wrong log paths** for C1/C3/C4: hardcoded as `/root/logs_cX/bot.log` in `main.py`'s `cmd_status()`. Low priority.
- **Coinglass liquidation endpoint** returns 500 with free/invalid key — test with real key. Add `COINGLASS_API_KEY` to .env.
- **C3/C4 poly_events=0** — Polymarket WebSocket feed dead on those two instances (as of 2026-04-07). Coinbase feed still active. Cause unknown.
- ~~**Watchdog does not monitor D-bots**~~ — DONE 2026-04-07.
- ~~**S6 always BALANCED**~~ — DONE 2026-04-07.
- ~~**S7 always NONE**~~ — DONE 2026-04-07.
- ~~**DOGE/XRP expansion blocked**~~ — DONE 2026-04-07.
- ~~**D-bot confidence calibration not built**~~ — DONE 2026-04-07.
- **Orphaned positions on restart** — when a D-bot restarts, in-memory positions are lost. Records remain in positions.jsonl on disk. Dashboard currently counts these as "open" — needs end_time > now filter.
- ~~**conf=0.00 on whale signals**~~ — FIXED 2026-04-09. Removed jaicobioas, 2ez4mee, MILKinDenial, 111111116 from whale_tracker.py entirely; scan16 will rediscover them if they're genuinely 99%+ WR.
- ~~**Dashboard open positions count inflated**~~ — FIXED 2026-04-09 (third session). Added `end_time > time.time()` filter in `/api/open_positions` before extending result list.
- **Dashboard balance history inaccurate** — reconstructed from positions.jsonl; orphaned positions and multiple bankroll resets corrupt the chart. Shared_bankroll.json is authoritative.
- ~~**D2 poly_executor bankroll bug**~~ — FIXED (confirmed present in deployed VPS copy at line 400: `self._bankroll_update(-cost_usd)` already in paper-mode open path).
- ~~**Shared bankroll value corrupted**~~ — RESET 2026-04-09. Reset again to $97.28 for final test.
- ~~**BiasTracker not yet built**~~ — DONE 2026-04-09. Built, integrated into W-bot and D2.
- ~~**BiasTracker not on D2**~~ — DONE 2026-04-09 (second session). `bias_tracker.py` copied to `/root/kalshiedge_dbot_d2/`, integrated into `d_main.py` (gate after choppiness, record in settlement).
- ~~**No dashboard start/stop controls**~~ — DONE 2026-04-09 (second session). `POST /api/bot/{d2|w}/start` and `/stop`. Flag-based watchdog pause. Reset endpoint at `POST /api/reset`.
- ~~**W-bot trading volume too low**~~ — FIXED 2026-04-09 (fifth session). Root causes: BiasTracker blocking 68% of signals (replaced with WalletCooldown), whale pool only 87 wallets (expanded to 240+ at 95%+ WR).
- ~~**D2 missing/unsettled positions**~~ — FIXED 2026-04-09 (fifth session). 9 expired DOGE Up positions failed to settle due to DNS failure on VPS. Force-settled 6 using httpx + direct jsonl write; 7th settled naturally. Bankroll $60.86→$89.40.
- **scan16 W pool may be stale** — runs every 6h via cron; between scans the pool only grows if manually triggered. Monitor pool size in watchdog logs.
- ~~**XRP not in D2 ASSETS**~~ — ADDED 2026-04-09 (fifth session). `ASSETS = ["BTC", "ETH", "DOGE", "XRP"]`. direction_signals.py updated with Kraken pair XXRPZUSD.
- ~~**Dashboard balance chart inaccurate**~~ — FIXED 2026-04-11 (eleventh session). `_balance_history()` uses close events only, delta=realized_pnl, starts from bankroll.json initial_balance. Three-way breakdown added: Total/Liquid/Deployed.
- ~~**ETH dragging WR**~~ — BLOCKED 2026-04-11 (eleventh session). ETH removed from D2 ASSETS; ETH Up+Down added to W-bot `_BLOCKED_COMBOS`. BTC Up also blocked on W.
- **Model v6 not yet trained** — training_data.jsonl expanded to 164,094 markets (was 95K for v5). Run ml_train.py to produce v6. ml_calibrate.py cron will continue hot-reloading.

---

> Session logs: [SESSIONS.md](https://github.com/SierraNevadapng/polymarket-bot/blob/main/SESSIONS.md)



## Shared Bankroll

- **File:** `/root/shared_bankroll.json`
- **Scope:** All D-bots (D1-D7) and Whalebot (W).
- **Locking:** `fcntl.flock` exclusive lock via `/root/shared_bankroll.json.lock`
- **Initialized:** $107.00 on 2026-04-07
- **Flow (W — correct):** open → deduct cost_usd; settle → credit contracts × exit_price
- **Flow (D-bots):** open → deducts cost_usd (bug was fixed in prior session); settle → credit contracts × exit_price
- **Current value:** ~$47.21 — sixth session (pre-EV-gate losses; EV gate deployed 2026-04-10 01:09 UTC).

---

## Directional Strategy Roadmap

| Phase | Status |
|-------|--------|
| Spec A — Validate (scan_directional_fast.py, D1=91.8% WR) | DONE |
| Spec B — Build signal engine (S5+S7 + direction_consensus + d_main) | DONE |
| Deploy D1-D7, paper trade, fix gates (ETH Up, 15-min, skip logging) | DONE |
| Add D-bots to watchdog, DOGE expansion, confidence calibration, dashboard | DONE |
| Fix S6/S7 bugs, add timing gates, pause C-bots for focused D-bot tuning | DONE |
| Fix timing gate bug, duration-aware gates, calibration reset, shared bankroll | DONE |
| Paper D1-D7 12-24h with all fixes — validate WR per level | CURRENT (D2 only) |
| Fix fcntl bug, pause D4, tighten SOL Up gate (0.85), session P&L alerts | DONE 2026-04-08 |
| Build Whalebot (ccc_engine, threshold_engine, w_main) | DONE 2026-04-08 |
| Deploy Whalebot to VPS | DONE 2026-04-08 |
| Fix D2 bugs (regime UnboundLocalError, pass-2 tie, execute() tuple), dashboard redesign | DONE 2026-04-08 |
| D2 + W clean 12-24h run — validate WR | DONE — W +84% in 12h; D2 ETH Down regime flip exposed |
| Discover D2 bankroll bug (no open deduction), full P&L reconciliation | DONE 2026-04-08 |
| Add UP_ONLY filter to W; superseded by BiasTracker plan | DONE 2026-04-08 |
| Pause D2, stop all bots for rebuild | DONE 2026-04-08 |
| Build BiasTracker (rolling WR circuit breaker per asset+direction) for W | DONE 2026-04-09 |
| Fix D2 poly_executor bankroll bug | DONE (was already fixed in prior session) |
| Reset shared bankroll to correct value | DONE 2026-04-09 — $84.76 |
| Deploy W-bot polymarket_data.py + liquidity/CLOB gates | DONE 2026-04-09 |
| Add BiasTracker to D2 | DONE 2026-04-09 |
| Dashboard start/stop controls + reset endpoint | DONE 2026-04-09 |
| Watchdog flag-based pause (d2_paused, w_paused) | DONE 2026-04-09 |
| Final test: W + D2 together from $97.28 shared bankroll | CURRENT |
| Fix ETH_UP_MIN_CONF gate (was in .env, not in code); add generic combo_min_conf helper | DONE 2026-04-09 |
| Fix dashboard open positions expired filter | DONE 2026-04-09 |
| IC analysis (814-trade backtest); S2 contrarian, S3 upweight deployed to D2 | DONE 2026-04-09 |
| Add S8 (options skew), S9 (CB premium), S10 (perp basis) signals | DONE 2026-04-09 |
| Dashboard time-range tabs (1H/24H/7D/30D/All) for balance history chart | DONE 2026-04-09 |
| Telegram entry messages: float n=X.X + fired signals list | DONE 2026-04-09 |
| D2 time-zone gates (dead/strict/peak/standard), MIN_CONF 0.55→0.65, peak Kelly 1.2× | DONE 2026-04-09 (fifth session) |
| Add XRP to D2 ASSETS + direction_signals.py Kraken pair XXRPZUSD | DONE 2026-04-09 (fifth session) |
| W-bot BiasTracker → WalletCooldown (3 losses → 45-min per-wallet cooldown) | DONE 2026-04-09 (fifth session) |
| W-bot S1-S10 directional filter (dir veto/boost) in on_whale_trade() | DONE 2026-04-09 (fifth session) |
| W-bot whale pool expanded to 95%+ WR (240+ wallets), W_POOL_CAP=500 | DONE 2026-04-09 (fifth session) |
| scan16 W pool updates every 20 new wallets; cron every 6h | DONE 2026-04-09 (fifth session) |
| Force-settle stuck positions via httpx + direct jsonl write | DONE 2026-04-09 (fifth session) |
| Build drift_ev.py latency arb module (Brownian bridge P(win), EV gate, wait_for_ev_window) | DONE 2026-04-10 (sixth session) |
| Add EV gate to D2 (after CLOB, conf boost on pass) | DONE 2026-04-10 (sixth session) |
| Add MIN_WHALE_ENTRY + EV gate + wait_for_ev_window to W-bot | DONE 2026-04-10 (sixth session) |
| Deploy Frankfurt droplet Binance proxy (138.197.181.139:8081, systemd service) | DONE 2026-04-10 (seventh session) |
| Download 36GB Polymarket dataset (jon-becker/prediction-market-analysis) | DONE 2026-04-10 (seventh session) |
| Filter dataset → 91,148 labeled crypto Up-or-Down markets (markets_updown.parquet) | DONE 2026-04-10 (seventh session) |
| Build ML pipeline: ml_features.py, ml_filter_dataset.py, ml_backfill.py, ml_train.py, ml_predict.py | DONE 2026-04-10 (seventh session) |
| Filter trade data from 40,454 parquet files → trades_updown.parquet + ask_price in markets_updown.parquet | DONE (eighth session) |
| Run ml_backfill.py — reconstruct signal features for 91K historical markets | DONE (ninth session) — parallel 8-worker, 9.6 min |
| Train XGBoost model on backfilled data | DONE (ninth session) — model_v5, CV AUC 0.774, threshold p≥0.70 |
| Wire ml_predict.py into d_main.py + w_main.py (local files updated) | DONE (eighth session) |
| Deploy model + updated d_main.py + w_main.py to VPS | DONE (ninth session) |
| Add ml_calibrate.py cron (30 min) for continuous retraining from live trades | DONE (ninth session) |
| Monitor live WR on first 200 trades — if 80%+ open Kelly fully | CURRENT |
| Re-run IC analysis after S8/S9/S10 + XRP accumulate enough trades; tune weights | pending |
| Monitor EV gate impact on WR and trade volume | pending |
| Implement CVD as live signal (S11) using Frankfurt proxy Binance aggTrades | DONE (tenth session) |
| Late-entry scanner (<30s remaining markets, near-guaranteed EV) | DONE (tenth session) |
| CoinbaseWS real-time price feed (eliminates REST latency) | DONE (tenth session) |
| Economic calendar blackout gate (NFP/CPI/FOMC/PCE) | DONE (tenth session) |
| AutocorrTracker (consecutive outcome confidence adjustment) | DONE (tenth session) |
| drift_ev.py resolution tightening (minute-align + Binance fallback) | DONE (tenth session) |
| Kelly correlation adjustment (corr matrix, _corr_discount()) | DONE (tenth session) |
| 24-48h paper validation with full stack → go live | CURRENT |
| 130-trade analysis: block ETH both dirs + BTC Up, tier Kelly (BTC Down 1.3×, default 0.8×) | DONE 2026-04-11 (eleventh session) |
| Dashboard balance chart accuracy fix (close-events only, bankroll.json initial, LA timezone) | DONE 2026-04-11 (eleventh session) |
| Dashboard three-way balance: Total/Liquid/Deployed | DONE 2026-04-11 (eleventh session) |
| ML backfill expanded to 164,094 markets → training_data.jsonl | DONE 2026-04-11 (eleventh session) |
| Train model v6 on 164K-market dataset | pending |
| Monitor 50-100 trades post-combo-cleanup — confirm W:L moves toward 1.3×+ | CURRENT |
| Go live (PAPER_TRADING=0, D2 + W .env restart) | next |
| Tabled for next week (need data): #3 hist patterns, #8 CLOB taker/maker, #10 VWAP, #11 funding extremes, #12 heatmap, #13 mean reversion | pending |

Spec files: `/root/polybot_backup/spec_d1_d6_consensus_system.md`, `spec_directional_wallet_scanner.md`
Whalebot spec: `C:\Users\gabri\docs\superpowers\specs\2026-04-07-whalebot-design.md`
Local files: `C:\tmp\scan_directional_fast.py`, `C:\tmp\direction_signals.py`, `C:\tmp\direction_consensus.py`, `C:\tmp\d_main_latest.py`
**GitHub:** https://github.com/SierraNevadapng/polymarket-bot (private) — remote configured on C:\tmp. Push: `git add -A && git commit -m "msg" && git push`
