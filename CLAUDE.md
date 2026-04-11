# Polymarket Whale Copy-Trading Bot

## What this is

A paper-mode (and optionally live) whale copy-trading system for [Polymarket](https://polymarket.com) Up-or-Down markets.

**Core insight:** Certain Polymarket wallets win 80-100% of their updown trades over hundreds of markets. By monitoring their on-chain activity in real time and requiring multiple whales to agree (consensus), we filter noise and copy only their highest-conviction signals.

**Current mode:** Paper trading — no real orders placed. Bankroll simulated at $107.

## Architecture

```
whale_tracker.py   Polygon WebSocket — monitors CTF Exchange OrderFilled events
                   Fires on_whale_trade(signal) when a tracked wallet buys
market_finder.py   Gamma-API + CLOB — maps token IDs to Up-or-Down markets
poly_executor.py   Kelly sizing, FOK paper orders, position tracking, P&L
main.py            Consensus filter, signal logging, Telegram alerts, settlement loop
telegram_alerts.py Async httpx Telegram client with command polling
analyze_signals.py Offline analysis — derive CCC threshold from logged signal data
direction_model.py Current (broken) direction signal — momentum+RSI, ~50% WR, BLOCKED by MIN_CONFIDENCE=0.65
```

## Bot instances (all running on VPS 68.183.55.155)

Four simultaneous instances (nohup):

| Instance | Dir | Whales | Consensus | Active Logs | Status |
|---|---|---|---|---|---|
| C1 | `/root/kalshiedge_whalewallet_c1/` | 25 | 1 must agree | `/root/logs_c1/` | LIVE |
| C2 | `/root/kalshiedge_whalewallet/` | 40 | 2 must agree | `/root/kalshiedge_whalewallet/logs/` | LIVE |
| C3 | `/root/kalshiedge_whalewallet_c3/` | 75 | 3 must agree | `/root/logs_c3/` | LIVE |
| C4 | `/root/kalshiedge_whalewallet_c4/` | 101 | 4 must agree | `/root/logs_c4/` | LIVE |

**IMPORTANT — Log path quirk:** C1/C3/C4 were started from `/root/` with nohup, so their
LOGS_DIR env var resolves relative to `/root/` not their bot directory. Active log files are:
- C1: `/root/logs_c1/signals.jsonl`, `/root/logs_c1/positions.jsonl`
- C2: `/root/kalshiedge_whalewallet/logs/signals.jsonl` (started from within its dir)
- C3: `/root/logs_c3/signals.jsonl`, `/root/logs_c3/positions.jsonl`
- C4: `/root/logs_c4/signals.jsonl`, `/root/logs_c4/positions.jsonl`

Old/stale log files also exist in subdirs (from previous sessions) — ignore them.

Each instance is fully independent with its own `.env`, logs, and positions file.

## Restart all bots (via paramiko)

```python
import paramiko, time
HOST, USER, PW = '68.183.55.155', 'root', 'BASILSK20$$'
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, username=USER, password=PW, allow_agent=False, look_for_keys=False, timeout=15)

bots = [
    ('C1', '/root/kalshiedge_whalewallet_c1', '/root/logs_c1/bot.log'),
    ('C2', '/root/kalshiedge_whalewallet',    '/root/kalshiedge_whalewallet/logs/bot.log'),
    ('C3', '/root/kalshiedge_whalewallet_c3', '/root/logs_c3/bot.log'),
    ('C4', '/root/kalshiedge_whalewallet_c4', '/root/logs_c4/bot.log'),
]
c.exec_command('pkill -f "python3.*main.py" 2>/dev/null; sleep 2')
time.sleep(3)
for bot, d, log in bots:
    shell = c.invoke_shell()
    time.sleep(0.5)
    shell.send(f'cd {d} && export $(cat .env | xargs) && nohup python3 main.py >> {log} 2>&1 & disown\n')
    time.sleep(2)
    shell.close()
```

**Do NOT redirect stdout to log file in exec_command** — use invoke_shell() + disown pattern.
**Do NOT use `2>&1 | tee`** — FileHandler in main.py handles logging, double-redirect creates duplicates.

## Watchdog (deployed 2026-04-06)

**`/root/watchdog.py`** — runs every 5 min via crontab. Checks all 6 critical processes and auto-restarts any that have died.

```
# Crontab entry:
*/5 * * * * python3 /root/watchdog.py >> /tmp/watchdog.log 2>&1
```

Monitored processes:
- 4 bot instances (C1/C2/C3/C4) — detected via `/proc/{pid}/cwd` exact directory match + `main.py` in cmdline
- `scan16_auto.py` — wallet discovery (MUST NEVER stop)
- `rotation_manager.py` — wallet rotation

If a process is dead, watchdog restarts it using the same `invoke_shell + disown` pattern as manual restarts. Logs to `/tmp/watchdog.log`.

**Constant scanning is a core operational requirement.** Target: 100K+ wallet pool. Scanning must never stop — watchdog enforces this automatically.

## Background scripts (always running, restart after VPS reboot)

| Script | Location | Output | Purpose |
|--------|----------|--------|---------|
| `scan16_auto.py` | `/root/kalshiedge_whalewallet/` | `/tmp/scan16_out.txt` | Discovers/scores whale wallets, auto-updates C3/C4 |
| `rotation_manager.py` | `/root/kalshiedge_whalewallet/` | `/tmp/rotation_out.txt` | Every 45 min: removes underperformers, promotes high-WR whales |

Restart after reboot (watchdog handles this automatically every 5 min):
```bash
nohup python3 /root/kalshiedge_whalewallet/scan16_auto.py > /tmp/scan16_out.txt 2>&1 &
nohup python3 /root/kalshiedge_whalewallet/rotation_manager.py > /tmp/rotation_out.txt 2>&1 &
```

**Scan pool status:** Pool file at `/tmp/scan16_pool.jsonl`. Check size: `wc -l /tmp/scan16_pool.jsonl`
- Pool feeds rotation_manager replacements — if pool is 0, rotation can remove underperformers but has no replacements to add
- scan16 thresholds: C3 needs 25 new 99%+ wallets; C4 needs 39 new 80%+ wallets before auto-updating

**rotation_manager log path bug (fixed 2026-04-05):** C1/C3/C4 log paths were wrong (pointed to subdirs inside bot dirs instead of `/root/logs_c1/` etc). Fixed in rotation_manager.py. Backup at `rotation_manager.py.bak2`.

## MIN_CONFIDENCE patch (applied 2026-04-05)

`MIN_CONFIDENCE` raised from `0.54` → `0.65` on all 4 `poly_executor.py` files.

- Direction trades fire at conf 0.59–0.62 → **now blocked**
- Whale consensus trades fire at conf 0.91 → **unaffected**
- Reason: direction model was 46.8–51.4% WR (coin flip minus fees), actively losing money
- Backups at `poly_executor.py.bak` in each bot directory

## Performance snapshot (17hr window, 2026-04-05)

| Bot | Whale WR | Whale Trades | Dir WR | Dir Trades |
|-----|----------|-------------|--------|------------|
| C1  | 64.8%    | 125         | 51.4%  | 72         |
| C2  | **78.2%**| 55          | 50.0%  | 72         |
| C3  | 68.3%    | 41          | 50.8%  | 63         |
| C4  | 65.0%    | 20          | 46.8%  | 62         |

Direction trades are now blocked. Whale trades are the only active signal.

## CCC — Combined Confidence Consensus

The current consensus filter (N whales must agree) is a temporary proxy. The real mechanism is **CCC**:

```
CCC = sum of confidence scores of all whales who signaled the same market+direction
      within CONSENSUS_WINDOW_SECS (120s)

Execute trade if CCC >= threshold
```

**Why:** 2 elite whales (CCC ~1.77) can outperform 3 mediocre ones. Quality > count.

**Status:** Signal data is being collected now. After 24-48 hours of C2+C3+C4 running, run `analyze_signals.py` to derive the threshold. Then replace the count filter with a CCC filter in `main.py`.

## Whale confidence scores

Assigned based on historical updown win rate from `data-api.polymarket.com/activity`:

| Win Rate | Confidence |
|---|---|
| 99%+ | 0.91–0.92 |
| 95–98% | 0.85–0.90 |
| 90–94% | 0.81–0.85 |
| 85–89% | 0.78–0.81 |
| 80–84% | 0.74–0.78 |
| 75–79% | 0.69–0.73 |
| 70–74% | 0.65–0.68 |
| 67–69% | 0.61–0.63 |

## Key files

| File | Purpose |
|---|---|
| `main.py` | Consensus filter (`_check_consensus`), signal logging (`_log_signal`), Telegram alerts |
| `whale_tracker.py` | `WHALE_ADDRESSES` + `WHALE_CONFIDENCE` dicts, Polygon WS subscription |
| `market_finder.py` | Token ID to market mapping. Gamma-API, refreshes on unknown tokens |
| `poly_executor.py` | Kelly sizing (capped 20% bankroll), paper/live FOK orders, `_settle()` |
| `telegram_alerts.py` | Alerts + `/status /positions /pnl /whales /markets /pause /resume` |
| `analyze_signals.py` | Run offline: solo WR, pairwise correlation, win rate by combo, CCC recommendation |

## Consensus filter (main.py)

```python
CONSENSUS_REQUIRED    = int(os.getenv("CONSENSUS_REQUIRED", "2"))
CONSENSUS_WINDOW_SECS = float(os.getenv("CONSENSUS_WINDOW_SECS", "120.0"))
BOT_LABEL             = os.getenv("BOT_LABEL", f"C{CONSENSUS_REQUIRED}")

# Buffer: {conditionId-direction: [(addr, name, conf, timestamp), ...]}
# Execute when len(distinct whales) >= CONSENSUS_REQUIRED
```

## Signal logging (main.py -> logs/signals.jsonl)

Every whale signal (executed or not) is logged:
```json
{
  "ts": 1743000000.0, "label": "C2",
  "whale_addr": "0x...", "whale_name": "scan_99wr_211n",
  "condition_id": "0x...", "direction": "Up",
  "symbol": "BTC-USD", "confidence": 0.92,
  "executed": true, "consensus_n": 2,
  "partners": ["scan_97wr_162n"], "trade_id": "abc123-Up-1743000000",
  "outcome": null
}
```
`outcome` is filled in by `analyze_signals.py` from `positions.jsonl` after settlement.

## Environment variables (.env per instance)

```
CONSENSUS_REQUIRED=2          # 2 for C2, 3 for C3, 4 for C4
LOGS_DIR=logs                 # logs for C2, logs_c3 for C3, logs_c4 for C4
BOT_LABEL=C2                  # tags all Telegram messages and log records
BANKROLL_USD=107
TELEGRAM_BOT_TOKEN=8778011576:AAHOqBb2Wz8VvBZn4_5IR8BgOctSLHpI8TM
TELEGRAM_CHAT_ID=6400219232
```

## Blockchain details

- **Network:** Polygon (chain ID 137)
- **CTF Exchange:** `0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e`
- **Event topic:** `0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6` (OrderFilled)
- **Buy detection:** whale gives USDC (asset ID = 0) = buying position token
- **Sell detection:** whale gives position token = exiting (ignored, not copied)
- **WebSocket RPC:** `wss://polygon-bor-rpc.publicnode.com`

## Risk controls (poly_executor.py)

```python
MAX_KELLY_FRACTION   = 0.20   # cap at 20% of bankroll per trade
MIN_CONFIDENCE       = 0.65   # ← PATCHED 2026-04-05 (was 0.54) — blocks direction trades
MAX_CONTRACTS_PAPER  = 25
MIN_MARKET_SECS_LEFT = 45     # skip if market expires in < 45s
MAX_OPEN_POSITIONS   = 10     # with dual 5+15m trades, this = 5 simultaneous whale signals
MIN_TRADE_DOLLARS    = 1.00
MIN_ASK_PRICE        = 0.70   # ← PATCHED 2026-04-06 (was 0.25) — only copy high-conviction whale entries
TAKER_FEE            = 0.01   # 1% Polymarket CLOB taker fee
```

**Why MIN_ASK_PRICE=0.70:** Analysis of 24h trade data showed whale edge only exists at expensive entries (>0.75):
- C1 >0.75 bucket: 91% WR | C2: 89% | C3: 81% | C4: 73%
- Cheap entries (<0.55) were 38–50% WR — no edge, pure drag on PnL
- Break-even at 0.75 entry = 76% WR, all bots hit this in the >0.75 bucket
- Whales buy heavy favorites when they have conviction — that's the signal worth copying

## Dual 5-min + 15-min execution (patched 2026-04-06)

Every whale consensus signal now fires **two trades**:
1. **5-min leg** — whale's own market (always entered first)
2. **15-min companion leg** — next active market for same symbol+direction with 400–1000s remaining

**Why:** 15-min WR was 78.6% vs 5-min at 64.4% (14 vs 222 trades). More exposure to higher-WR window.

**market_finder.py:** SLUG_INTERVALS now only `5m` and `15m` — 30-min and 4-hour removed.
- 30-min/4-hour: capital tied up too long, no edge data, slower compounding
- 5-min positions settle in 5 min and free capital for next signal

**main.py:** After executing 5-min leg, searches for companion market with `end_time` in 400–1000s window. Fires separately via executor with `source=whale:{name}:15m`. Independent Kelly sizing per leg.

**MAX_OPEN_POSITIONS=10** kept — with dual trades, this allows 5 simultaneous whale signals active at once which is correct for Kelly math.

## Logs

| File | Contents |
|---|---|
| `logs/bot.log` | All stdout from the bot process |
| `logs/signals.jsonl` | Every whale signal (executed + skipped) — CCC training data |
| `logs/positions.jsonl` | Every trade open/close with P&L — ground truth for analysis |

## Wallet scanning

Whale wallets are discovered by scraping Polymarket pages for addresses, then scoring
each via `data-api.polymarket.com/activity` historical updown trades.

Quality bar: **>=67% WR, n>=8 settled updown trades, positive PnL, PnL>$5**

**Target: 100K+ wallet pool is the baseline. Scanning must never stop.**

| Scan | Candidates scored | Qualifiers | Notes |
|---|---|---|---|
| scan5 | 1,372 | 44 | Initial: activity, leaderboard, homepage |
| scan6 | 9,986 | 94 | Added: category pages, event pages, gamma events |
| scan16 | running | TBD | Auto-updates C3/C4 when thresholds met; deep harvests all BTC/ETH/SOL Up-Down trade histories |

scan16 sources:
- `harvest_from_updown_markets()`: All BTC/ETH/SOL Up-Down markets (active + resolved) via gamma API + data-api/trades — pulls every trader who ever participated
- `harvest_from_clob()`: CLOB live trade feed
- Persistent pending queue: `/tmp/scan16_pending.txt` — survives restarts
- Deep harvest every 5 rounds; scores 300 addresses/round with 5s rest if queue > 100

Scripts: `/root/kalshiedge_whalewallet/_scan5.py`, `_scan6.py`, `scan16_auto.py`

## Backup system (added 2026-04-05)

All code + jsonl data backed up to `/root/polybot_backup/` (git repo).

- **Auto-backup:** cron job every 30 min via `/root/polybot_backup.sh`
- **Backup includes:** all 4 bot directories, scan16_auto.py, rotation_manager.py, all positions.jsonl + signals.jsonl
- **GitHub push:** ready — just add remote: `cd /root/polybot_backup && git remote add origin <your-repo-url> && git push -u origin master`
- **Local copies:** specs and analysis at `C:\tmp\`

Files in backup:
```
/root/polybot_backup/
  c1/  c2/  c3/  c4/           ← full bot code (no .log files)
  logs_backup/                  ← all positions.jsonl + signals.jsonl
  scan16_auto.py
  rotation_manager.py
  spec_directional_wallet_scanner.md
  spec_d1_d6_consensus_system.md
  bot_analysis_report.md
```

## Roadmap

### Next: D-system (directional consensus)

Two specs written and saved in `/root/polybot_backup/`:

**Spec A — Directional Wallet Scanner** (`spec_directional_wallet_scanner.md`)
- Scans Polymarket history to find wallets profitably trading BTC/ETH/SOL Up-Down
- Target archetype: `0xe1D6b51521Bd4365769199f392F9818661BD907` (+$220K, 9,837 trades, buys cheap sides 8-41¢)
- Pulls 6 signal states (price action, order flow, funding rate, OI, liquidations, Polymarket flow) for each historical trade
- Output: `directional_trades.jsonl` — backtest dataset for D1–D6 validation

**Spec B — D1–D6 Consensus System** (`spec_d1_d6_consensus_system.md`)
- 6 independent signals: price action, order flow, market structure, liquidations, cross-asset, Polymarket flow
- N signals must agree within 60s window to fire
- D3 is primary production level (expected ~70% WR)
- Mirrors C1–C4 architecture exactly
- Launch D3 paper first → validate → expand to D1/D2/D4/D5/D6

**Implementation order:**
1. Run Spec A scanner → validate signal WRs from backtest data
2. Build Spec B Phase 1: S4 liquidations + S2 order flow + consensus scaffold
3. Paper trade D3 for 48–72 hours
4. If WR ≥ 68%, expand

## CRITICAL SAFETY RULES — NEVER VIOLATE

**NEVER write to, overwrite, or delete any of these files without explicit YES/NO approval from the user:**

- `whale_tracker.py` (any bot directory)
- `main.py` (any bot directory)
- `poly_executor.py` (any bot directory)
- `market_finder.py` (any bot directory)
- `.env` (any bot directory)
- Any file in `logs/`, `logs_c1/`, `logs_c2/`, `logs_c3/`, `logs_c4/`
- Any file in `/root/polybot_backup/logs_backup/`

**Before running any command that writes to a bot file:**
1. State exactly what will change
2. Ask "Confirm? (yes/no)"
3. Wait for explicit "yes" before proceeding

**Never chain file writes with restarts in a single command** — always separate into discrete steps so the user can review each one.

This rule exists because a `printf` command previously wiped all 4 `whale_tracker.py` files to 0 bytes, destroying the wallet lists.

## Operational notes

- VPS: DigitalOcean, 68.183.55.155, 1GB RAM + 2GB swap
- Password: BASILSK20$$
- `pip install` requires `--break-system-packages` (Debian externally-managed)
- Always use `nohup` for long-running scans: `nohup python3 script.py > /tmp/out.txt 2>&1 &`
- Four bots + 2 background scripts use ~700MB RAM — within swap headroom
- SSH via paramiko: `allow_agent=False, look_for_keys=False`
- **Do not touch** `/root/kalshiedge/` — live Kalshi arb bot with real money
- **Do not modify** `coinbase_feed.py` or `signal_detector.py` in any bot
- **Do not add retry logic** to any `POST /portfolio/orders` call
