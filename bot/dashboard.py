"""
Polymarket Bot Dashboard — FastAPI + embedded HTML
Tracks: D2 (directional) + W (whalebot)
Deploy: /root/dashboard.py
Run: nohup uvicorn dashboard:app --host 0.0.0.0 --port 8080 > /tmp/dashboard.log 2>&1 &
"""

import asyncio
import fcntl
import json
import logging
import os
import signal as _signal
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("dashboard")

app = FastAPI(title="Polymarket Bot Dashboard")

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT = Path("/root")

BOTS = {
    "d2": {
        "label": "D2",
        "dir": ROOT / "kalshiedge_dbot_d2",
        "log": ROOT / "kalshiedge_dbot_d2" / "logs_d2" / "bot.log",
        "positions": ROOT / "kalshiedge_dbot_d2" / "logs_d2" / "positions.jsonl",
        "signals": ROOT / "kalshiedge_dbot_d2" / "logs_d2" / "signals.jsonl",
        "cwd": "/root/kalshiedge_dbot_d2",
        "cmd_needle": "d_main",
    },
    "w": {
        "label": "W",
        "dir": ROOT / "kalshiedge_whalebot",
        "log": ROOT / "kalshiedge_whalebot" / "logs_w" / "bot.log",
        "positions": ROOT / "kalshiedge_whalebot" / "logs_w" / "positions.jsonl",
        "signals": ROOT / "kalshiedge_whalebot" / "logs_w" / "signals.jsonl",
        "cwd": "/root/kalshiedge_whalebot",
        "cmd_needle": "w_main",
    },
}

SHARED_BANKROLL = ROOT / "shared_bankroll.json"
WHALE_POOL      = ROOT / "kalshiedge_whalebot" / "whale_pool.json"
WALLET_CAL      = ROOT / "kalshiedge_whalebot" / "wallet_calibration.json"

BOT_START_CMDS = {
    "d2": "cd /root/kalshiedge_dbot_d2 && set -a && source .env && set +a && nohup python3 d_main.py >> logs_d2/bot.log 2>&1 &",
    "w":  "cd /root/kalshiedge_whalebot && set -a && source .env && set +a && nohup python3 w_main.py >> logs_w/bot.log 2>&1 &",
}
BOT_PAUSE_FLAGS = {
    "d2": ROOT / "d2_paused",
    "w":  ROOT / "w_paused",
}

_regime_cache: dict = {"data": None, "ts": 0.0}
REGIME_TTL = 300

# ── Live Polymarket account tracking ──────────────────────────────────────────
# Actual maker address (resolved via getPolyProxyWalletAddress from EOA)
MAKER_ADDRESS        = os.getenv("POLYMARKET_ADDRESS_MAKER", "0xEf5750e0787C23e7540110ca54D1e098bdC4C9DF")
POLYGON_RPC_URL      = "https://polygon-bor-rpc.publicnode.com"
USDC_CONTRACT        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ACTIVITY_URL         = "https://data-api.polymarket.com/activity"

_live_balance_cache: dict = {"ts": 0.0, "balance": None}
LIVE_BALANCE_TTL = 60   # seconds between on-chain RPC calls

# ── Live on-chain helpers ─────────────────────────────────────────────────────

async def _fetch_live_usdc_balance() -> Optional[float]:
    """Query Polygon RPC for USDC balance of the actual maker address."""
    now = time.time()
    if now - _live_balance_cache["ts"] < LIVE_BALANCE_TTL and _live_balance_cache["balance"] is not None:
        return _live_balance_cache["balance"]
    try:
        padded  = MAKER_ADDRESS[2:].lower().zfill(64)
        payload = {
            "jsonrpc": "2.0", "method": "eth_call",
            "params":  [{"to": USDC_CONTRACT, "data": "0x70a08231" + padded}, "latest"],
            "id": 1,
        }
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(POLYGON_RPC_URL, json=payload)
            result  = r.json().get("result", "0x0")
            balance = int(result, 16) / 1e6
        _live_balance_cache["ts"]      = now
        _live_balance_cache["balance"] = round(balance, 4)
        return _live_balance_cache["balance"]
    except Exception as e:
        logger.warning(f"[live_balance] RPC failed: {e}")
        return _live_balance_cache.get("balance")


async def _reconcile_polymarket_activity() -> int:
    """
    Poll Polymarket activity API for MAKER_ADDRESS.
    For every open position whose end_time has passed, check if a SELL/REDEEM
    event exists and write a close event to positions.jsonl.
    Returns number of positions settled.
    """
    settled = 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(ACTIVITY_URL, params={"user": MAKER_ADDRESS, "limit": 200})
        if r.status_code != 200:
            return 0
        activity = r.json()
    except Exception as e:
        logger.warning(f"[reconcile] activity fetch failed: {e}")
        return 0

    # Index sells/redeems by token_id
    sells: dict = {}
    for ev in activity:
        asset    = str(ev.get("asset", ""))
        ev_type  = ev.get("type", "")
        ev_side  = str(ev.get("side", "")).upper()
        is_sell  = (ev_type == "TRADE" and ev_side == "SELL") or ev_type == "REDEEM"
        if is_sell:
            sells.setdefault(asset, []).append(ev)

    now = time.time()

    for bot_key, cfg in BOTS.items():
        pos_file = cfg["positions"]
        if not pos_file.exists():
            continue

        open_map:  dict = {}
        close_ids: set  = set()
        try:
            with open(pos_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    pid = rec.get("id", "")
                    if rec.get("event") == "open":
                        open_map[pid] = rec
                    elif rec.get("event") == "close":
                        close_ids.add(pid)
        except Exception:
            continue

        to_write = []
        for pid, pos in open_map.items():
            if pid in close_ids:
                continue
            end_time = float(pos.get("end_time", 0))
            if end_time > now + 30:        # not yet expired
                continue
            token_id  = str(pos.get("token_id", ""))
            ts_open   = float(pos.get("ts_open", 0))
            cost_usd  = float(pos.get("cost_usd", 0))

            relevant = [s for s in sells.get(token_id, [])
                        if float(s.get("timestamp", 0)) >= ts_open - 5]

            if relevant:
                payout       = sum(float(s.get("usdcSize", 0)) for s in relevant)
                ts_close     = max(float(s.get("timestamp", 0)) for s in relevant)
                exit_ct      = sum(float(s.get("size", 0)) for s in relevant)
                exit_price   = round(payout / exit_ct, 6) if exit_ct > 0 else 0.99
                pnl          = round(payout - cost_usd, 4)
                close_ev     = {**pos, "event": "close",
                                "status": "won" if pnl > 0 else "lost",
                                "exit_price": exit_price, "realized_pnl": pnl,
                                "ts_close": ts_close, "source": "dashboard_reconcile"}
            elif end_time > 0 and end_time < now - 300:
                # 5 min past expiry, no sell found → expired worthless
                close_ev = {**pos, "event": "close", "status": "lost",
                            "exit_price": 0.0, "realized_pnl": round(-cost_usd, 4),
                            "ts_close": end_time, "source": "dashboard_reconcile"}
            else:
                continue

            to_write.append(close_ev)

        if to_write:
            lock_path = str(pos_file) + ".lock"
            try:
                with open(lock_path, "w") as lf:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                    try:
                        with open(pos_file, "a", encoding="utf-8") as f:
                            for ev in to_write:
                                f.write(json.dumps(ev) + "\n")
                        settled += len(to_write)
                        logger.info(f"[reconcile] {bot_key}: auto-settled {len(to_write)} positions")
                    finally:
                        fcntl.flock(lf, fcntl.LOCK_UN)
            except Exception as e:
                logger.error(f"[reconcile] write failed for {bot_key}: {e}")

    return settled


async def _reconcile_loop():
    """Background task: reconcile open positions against Polymarket every 2 min."""
    await asyncio.sleep(10)   # brief startup delay
    while True:
        try:
            n = await _reconcile_polymarket_activity()
            if n:
                logger.info(f"[reconcile] settled {n} positions")
        except Exception as e:
            logger.error(f"[reconcile] loop error: {e}")
        await asyncio.sleep(120)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_reconcile_loop())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_running(cwd: str, cmd_needle: str) -> bool:
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                if os.readlink(f"/proc/{pid}/cwd") != cwd:
                    continue
                raw = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\x00")
                if any(cmd_needle.encode() in part for part in raw):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _kill_bot(cwd: str, cmd_needle: str):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.readlink(f"/proc/{pid}/cwd") != cwd:
                continue
            raw = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\x00")
            if any(cmd_needle.encode() in part for part in raw):
                os.kill(int(pid), 9)
        except Exception:
            continue


def _bankroll() -> dict:
    try:
        data = json.loads(SHARED_BANKROLL.read_text())
        bal  = float(data.get("balance", 0))
        init = float(data.get("initial_balance", 97.28))
        return {"balance": round(bal, 2), "initial": round(init, 2), "delta": round(bal - init, 2)}
    except Exception:
        return {"balance": 0.0, "initial": 97.28, "delta": 0.0}


def _compute_balances() -> dict:
    """
    Compute total, liquid, and deployed balances from positions.jsonl.
      total    = initial_balance + sum(realized_pnl from all close events)
      deployed = sum(cost_usd for currently open positions)
      liquid   = total - deployed
    """
    try:
        br_data = json.loads(SHARED_BANKROLL.read_text())
        initial = float(br_data.get("initial_balance", 97.28))
    except Exception:
        initial = 97.28

    realized_pnl   = 0.0
    open_costs: dict = {}   # position id -> cost_usd

    for k in BOTS:
        pos_file = BOTS[k]["positions"]
        if not pos_file.exists():
            continue
        try:
            with open(pos_file, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    pid = rec.get("id", "")
                    if rec.get("event") == "open":
                        if pid:
                            open_costs[pid] = float(rec.get("cost_usd", 0))
                    elif rec.get("event") == "close":
                        realized_pnl += float(rec.get("realized_pnl", 0))
                        open_costs.pop(pid, None)
        except Exception:
            pass

    total    = round(initial + realized_pnl, 2)
    deployed = round(sum(open_costs.values()), 2)
    liquid   = round(total - deployed, 2)

    return {
        "total":        total,
        "liquid":       liquid,
        "deployed":     deployed,
        "initial":      round(initial, 2),
        "realized_pnl": round(realized_pnl, 2),
    }


def _bot_stats(bot_key: str) -> dict:
    cfg      = BOTS[bot_key]
    pos_file = cfg["positions"]
    wins = losses = open_pos = 0
    total_pnl  = 0.0
    last_trade = None

    if pos_file.exists():
        try:
            with open(pos_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                event  = rec.get("event", "")
                status = rec.get("status", "")
                if event == "open" and status == "open":
                    open_pos += 1
                elif event == "close":
                    pnl = float(rec.get("realized_pnl", 0))
                    total_pnl += pnl
                    if status == "won":
                        wins += 1
                    elif status == "lost":
                        losses += 1
                    if last_trade is None or rec.get("ts_close", 0) > last_trade.get("ts_close", 0):
                        last_trade = rec
        except Exception:
            pass

    total_closed = wins + losses
    wr = (wins / total_closed * 100) if total_closed > 0 else 0.0
    running = _is_running(cfg["cwd"], cfg["cmd_needle"])

    return {
        "running":      running,
        "wins":         wins,
        "losses":       losses,
        "open":         open_pos,
        "total_closed": total_closed,
        "wr":           round(wr, 1),
        "pnl":          round(total_pnl, 2),
        "last_trade":   last_trade,
    }


def _balance_history(bot_key: Optional[str] = None) -> list:
    """
    Reconstruct running balance from settled (close) events in positions.jsonl.
    Uses initial_balance from shared_bankroll.json as the starting point.
    Returns list of [ts_ms, balance] sorted ascending.
    """
    try:
        br_data = json.loads(SHARED_BANKROLL.read_text())
        INITIAL = float(br_data.get("initial_balance", 97.28))
    except Exception:
        INITIAL = 97.28

    events = []

    keys = [bot_key] if bot_key else list(BOTS.keys())
    for k in keys:
        pos_file = BOTS[k]["positions"]
        if not pos_file.exists():
            continue
        try:
            with open(pos_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    # Only use close events — track realized_pnl only (not payout)
                    if rec.get("event") == "close":
                        ts = rec.get("ts_close", 0)
                        pnl = float(rec.get("realized_pnl", 0))
                        if ts and pnl != 0:
                            events.append((ts * 1000 if ts < 1e12 else ts, pnl))
        except Exception:
            pass

    if not events:
        return [[int(time.time() * 1000) - 1, round(INITIAL, 2)]]

    events.sort(key=lambda x: x[0])
    balance = INITIAL
    result  = [[int(events[0][0]) - 1, round(balance, 2)]]
    for ts_ms, delta in events:
        balance += delta
        result.append([int(ts_ms), round(balance, 2)])
    return result


async def _fetch_regime() -> dict:
    now = time.time()
    if now - _regime_cache["ts"] < REGIME_TTL and _regime_cache["data"]:
        return _regime_cache["data"]
    try:
        end_ts   = int(now)
        start_ts = end_ts - 6 * 3600
        url = (
            f"https://api.exchange.coinbase.com/products/BTC-USD/candles"
            f"?granularity=3600&start={start_ts}&end={end_ts}"
        )
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(url)
            r.raise_for_status()
            raw = r.json()
        candles = sorted(raw, key=lambda x: x[0])
        closes  = [c[4] for c in candles]
        result: dict = {"regime": "Ranging", "error": None}
        if len(closes) >= 5:
            current = closes[-1]
            ref     = closes[-5]
            pct     = (current - ref) / ref * 100
            result["regime"]        = "Up" if pct > 1.5 else ("Down" if pct < -1.5 else "Ranging")
            result["current_price"] = round(current, 0)
            result["pct_change"]    = round(pct, 2)
        _regime_cache["data"] = result
        _regime_cache["ts"]   = now
        return result
    except Exception as e:
        return _regime_cache.get("data") or {"regime": "Unknown", "error": str(e)}


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    regime       = await _fetch_regime()
    br           = _bankroll()
    balances     = _compute_balances()
    d2           = _bot_stats("d2")
    w            = _bot_stats("w")
    live_balance = await _fetch_live_usdc_balance()
    return JSONResponse({
        "ts":             int(time.time()),
        "regime":         regime.get("regime", "Unknown"),
        "regime_pct":     regime.get("pct_change"),
        "btc_price":      regime.get("current_price"),
        "bankroll":       br,
        "balances":       balances,
        "live_usdc":      live_balance,
        "combined_pnl":   round(d2["pnl"] + w["pnl"], 2),
        "bots":           {"d2": d2, "w": w},
    })


@app.get("/api/trades")
async def api_trades():
    trades = []
    for bot_key, cfg in BOTS.items():
        pos_file = cfg["positions"]
        if not pos_file.exists():
            continue
        try:
            with open(pos_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("event") == "close":
                        rec["_bot"] = cfg["label"]
                        trades.append(rec)
        except Exception:
            pass
    trades.sort(key=lambda x: x.get("ts_close", 0), reverse=True)
    return JSONResponse(trades[:100])


@app.get("/api/balance_history")
async def api_balance_history(bot: str = "all", hours: float = 0):
    key = None if bot == "all" else bot
    history = _balance_history(key)
    if hours > 0:
        cutoff_ms = (time.time() - hours * 3600) * 1000
        # Keep the last point before cutoff as starting anchor, then all after
        before = [p for p in history if p[0] < cutoff_ms]
        after  = [p for p in history if p[0] >= cutoff_ms]
        anchor = [before[-1]] if before else []
        history = anchor + after
    return JSONResponse(history)


@app.get("/api/open_positions")
async def api_open_positions():
    result = []
    for bot_key, cfg in BOTS.items():
        pos_file = cfg["positions"]
        if not pos_file.exists():
            continue
        try:
            open_map = {}
            with open(pos_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    pos_id = rec.get("id", "")
                    event  = rec.get("event", "")
                    if event == "open":
                        open_map[pos_id] = {**rec, "_bot": cfg["label"]}
                    elif event == "close":
                        open_map.pop(pos_id, None)
            now = time.time()
            result.extend(p for p in open_map.values() if p.get("end_time", 0) > now)
        except Exception:
            pass
    result.sort(key=lambda x: x.get("ts_open", 0))
    return JSONResponse(result)


@app.post("/api/bot/{bot}/stop")
async def api_stop(bot: str):
    if bot not in BOTS:
        raise HTTPException(404, "Unknown bot")
    BOT_PAUSE_FLAGS[bot].touch()
    await asyncio.sleep(1)  # ensure watchdog sees flag before process dies
    cfg = BOTS[bot]
    _kill_bot(cfg["cwd"], cfg["cmd_needle"])
    return JSONResponse({"ok": True, "running": False})


@app.post("/api/bot/{bot}/start")
async def api_start(bot: str):
    if bot not in BOTS:
        raise HTTPException(404, "Unknown bot")
    try:
        BOT_PAUSE_FLAGS[bot].unlink()
    except FileNotFoundError:
        pass
    cfg = BOTS[bot]
    if not _is_running(cfg["cwd"], cfg["cmd_needle"]):
        subprocess.Popen(["bash", "-c", BOT_START_CMDS[bot]])
    return JSONResponse({"ok": True, "running": True})


@app.post("/api/reset")
async def api_reset():
    """Clear all positions/signals data and reset bankroll to $97.28."""
    for cfg in BOTS.values():
        for key in ("positions", "signals"):
            try:
                cfg[key].write_text("")
            except Exception:
                pass
    SHARED_BANKROLL.write_text(json.dumps({"balance": 97.28, "initial_balance": 97.28}))
    return JSONResponse({"ok": True})


@app.get("/api/log/{bot}")
async def api_log(bot: str):
    if bot not in BOTS:
        raise HTTPException(404, "Unknown bot")
    log_file = BOTS[bot]["log"]
    if not log_file.exists():
        return JSONResponse({"lines": []})
    try:
        lines = log_file.read_text(errors="replace").splitlines()[-200:]
        return JSONResponse({"lines": lines})
    except Exception as e:
        raise HTTPException(500, str(e))


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Alpha</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:       #07090E;
    --surface:  #0D111A;
    --surface2: #111620;
    --line:     #1C2233;
    --line2:    #252D3F;
    --text:     #E2E8F3;
    --muted:    #4A5568;
    --dim:      #2D3748;
    --gold:     #C9A84C;
    --gold-dim: #7A6128;
    --green:    #10B981;
    --green-bg: rgba(16,185,129,.08);
    --red:      #EF4444;
    --red-bg:   rgba(239,68,68,.08);
    --blue:     #3B82F6;
    --neutral:  #94A3B8;
  }

  html, body { height: 100%; background: var(--bg); color: var(--text);
    font-family: 'Inter', system-ui, sans-serif; font-size: 13px;
    -webkit-font-smoothing: antialiased; }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 4px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--line2); border-radius: 2px; }

  /* ── Header ── */
  header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(7,9,14,.92); backdrop-filter: blur(16px);
    border-bottom: 1px solid var(--line);
    padding: 0 28px;
    height: 52px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .logo {
    display: flex; align-items: center; gap: 10px;
    font-weight: 700; font-size: 14px; letter-spacing: .08em;
    color: var(--text);
  }
  .logo-mark {
    width: 28px; height: 28px; border-radius: 6px;
    background: linear-gradient(135deg, var(--gold), #8B6820);
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 700; color: #000;
  }
  .logo-sub { color: var(--muted); font-weight: 400; font-size: 11px; margin-top: 1px; letter-spacing: .04em; }
  .header-right { display: flex; align-items: center; gap: 20px; }
  .live-badge {
    display: flex; align-items: center; gap: 6px;
    font-size: 11px; font-weight: 600; letter-spacing: .06em;
    color: var(--green);
  }
  .live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green);
    animation: pulse 2s infinite; }
  @keyframes pulse {
    0%,100% { opacity: 1; } 50% { opacity: .4; }
  }
  .header-time { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
  .regime-pill {
    font-size: 10px; font-weight: 600; letter-spacing: .06em;
    padding: 3px 8px; border-radius: 100px;
  }
  .regime-up { background: var(--green-bg); color: var(--green); border: 1px solid rgba(16,185,129,.2); }
  .regime-down { background: var(--red-bg); color: var(--red); border: 1px solid rgba(239,68,68,.2); }
  .regime-flat { background: rgba(148,163,184,.06); color: var(--neutral); border: 1px solid var(--line); }

  /* ── Main layout ── */
  main { max-width: 1280px; margin: 0 auto; padding: 28px 28px 60px; }

  /* ── Section label ── */
  .section-label {
    font-size: 10px; font-weight: 600; letter-spacing: .12em;
    color: var(--muted); text-transform: uppercase; margin-bottom: 14px;
  }

  /* ── Hero stats row ── */
  .hero { display: grid; grid-template-columns: repeat(5, 1fr); gap: 1px;
    background: var(--line); border: 1px solid var(--line); border-radius: 10px;
    overflow: hidden; margin-bottom: 28px; }
  .hero-cell {
    background: var(--surface); padding: 20px 24px;
    display: flex; flex-direction: column; gap: 6px;
  }
  .hero-cell:first-child { border-radius: 10px 0 0 10px; }
  .hero-cell:last-child  { border-radius: 0 10px 10px 0; }
  .hero-label { font-size: 10px; font-weight: 600; letter-spacing: .1em;
    color: var(--muted); text-transform: uppercase; }
  .hero-value { font-size: 26px; font-weight: 700; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums; color: var(--text); line-height: 1; }
  .hero-sub { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .gold { color: var(--gold); }

  /* ── Two-column grid ── */
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 28px; }
  @media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }

  /* ── Strategy card ── */
  .strategy-card {
    background: var(--surface); border: 1px solid var(--line);
    border-radius: 10px; overflow: hidden;
  }
  .card-header {
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between;
  }
  .card-title { font-weight: 600; font-size: 13px; display: flex; align-items: center; gap: 10px; }
  .strategy-icon {
    width: 30px; height: 30px; border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700;
  }
  .icon-d2 { background: rgba(59,130,246,.12); color: var(--blue); border: 1px solid rgba(59,130,246,.2); }
  .icon-w  { background: rgba(201,168,76,.10); color: var(--gold); border: 1px solid rgba(201,168,76,.2); }
  .status-badge {
    font-size: 9px; font-weight: 700; letter-spacing: .08em; text-transform: uppercase;
    padding: 3px 8px; border-radius: 100px;
  }
  .status-live { background: var(--green-bg); color: var(--green); border: 1px solid rgba(16,185,129,.2); }
  .status-off  { background: rgba(100,116,139,.08); color: var(--muted); border: 1px solid var(--line); }
  .card-body { padding: 20px; }
  .stat-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 18px; }
  .stat-item {}
  .stat-label { font-size: 10px; color: var(--muted); font-weight: 500; letter-spacing: .06em;
    text-transform: uppercase; margin-bottom: 4px; }
  .stat-value { font-size: 18px; font-weight: 700; font-variant-numeric: tabular-nums;
    letter-spacing: -.02em; }
  .stat-sub { font-size: 11px; color: var(--muted); }
  .divider { height: 1px; background: var(--line); margin: 18px 0; }
  .last-trade {
    display: flex; align-items: center; justify-content: space-between;
    font-size: 11px;
  }
  .last-trade-label { color: var(--muted); }
  .last-trade-val { font-variant-numeric: tabular-nums; }
  .card-footer {
    padding: 12px 20px; border-top: 1px solid var(--line);
    display: flex; gap: 8px; justify-content: flex-end;
  }
  .btn {
    font-size: 11px; font-weight: 600; padding: 6px 14px; border-radius: 6px;
    border: 1px solid var(--line2); background: var(--surface2); color: var(--text);
    cursor: pointer; transition: all .15s; letter-spacing: .03em;
  }
  .btn:hover { background: var(--line); }
  .btn-danger { border-color: rgba(239,68,68,.3); color: var(--red); background: var(--red-bg); }
  .btn-danger:hover { background: rgba(239,68,68,.15); }
  .btn-start { border-color: rgba(16,185,129,.3); color: var(--green); background: var(--green-bg); }
  .btn-start:hover { background: rgba(16,185,129,.15); }

  /* ── Chart section ── */
  .chart-section {
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    margin-bottom: 28px; overflow: hidden;
  }
  .chart-header {
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between;
  }
  .chart-title { font-weight: 600; font-size: 13px; }
  .chart-tabs { display: flex; gap: 2px; }
  .chart-tab {
    font-size: 11px; font-weight: 500; padding: 5px 12px; border-radius: 6px;
    border: 1px solid transparent; color: var(--muted); cursor: pointer; transition: all .15s;
  }
  .chart-tab.active { background: var(--surface2); border-color: var(--line2); color: var(--text); }
  .chart-tab:hover:not(.active) { color: var(--text); }
  .chart-body { padding: 20px; position: relative; height: 240px; }

  /* ── Open positions ── */
  .open-section {
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    margin-bottom: 28px; overflow: hidden;
  }
  .open-header {
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between;
  }
  .count-badge {
    font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 100px;
    background: var(--gold-dim); color: var(--gold); letter-spacing: .04em;
  }
  .open-table { width: 100%; border-collapse: collapse; }
  .open-table th {
    padding: 10px 20px; font-size: 10px; font-weight: 600; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted); text-align: left;
    border-bottom: 1px solid var(--line); background: var(--surface);
  }
  .open-table td { padding: 12px 20px; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
  .open-table tr:last-child td { border-bottom: none; }
  .open-table tr:hover td { background: var(--surface2); }
  .empty-state {
    padding: 40px 20px; text-align: center; color: var(--muted); font-size: 12px;
  }

  /* ── Trade history ── */
  .trades-section {
    background: var(--surface); border: 1px solid var(--line); border-radius: 10px;
    overflow: hidden;
  }
  .trades-header {
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between;
  }
  .trades-table { width: 100%; border-collapse: collapse; }
  .trades-table th {
    padding: 10px 16px; font-size: 10px; font-weight: 600; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted); text-align: left;
    border-bottom: 1px solid var(--line); background: var(--surface); position: sticky; top: 52px;
  }
  .trades-table td { padding: 11px 16px; border-bottom: 1px solid var(--line);
    font-variant-numeric: tabular-nums; }
  .trades-table tr:last-child td { border-bottom: none; }
  .trades-table tr:hover td { background: var(--surface2); }
  .outcome-pill {
    font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 4px; letter-spacing: .04em;
  }
  .outcome-won  { background: var(--green-bg); color: var(--green); }
  .outcome-lost { background: var(--red-bg); color: var(--red); }
  .bot-tag {
    font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 4px; letter-spacing: .03em;
  }
  .bot-d2 { background: rgba(59,130,246,.1); color: var(--blue); }
  .bot-w  { background: rgba(201,168,76,.1); color: var(--gold); }
  .dir-up   { color: var(--green); font-weight: 600; }
  .dir-down { color: var(--red);   font-weight: 600; }

  /* ── Log modal ── */
  .modal-overlay {
    display: none; position: fixed; inset: 0; z-index: 200;
    background: rgba(0,0,0,.75); backdrop-filter: blur(4px);
    align-items: center; justify-content: center;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--surface); border: 1px solid var(--line2);
    border-radius: 10px; width: 800px; max-width: 95vw; max-height: 80vh;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .modal-header {
    padding: 16px 20px; border-bottom: 1px solid var(--line);
    display: flex; align-items: center; justify-content: space-between; flex-shrink: 0;
  }
  .modal-title { font-weight: 600; font-size: 13px; }
  .modal-close { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 18px; }
  .modal-close:hover { color: var(--text); }
  .modal-body { overflow-y: auto; padding: 16px 20px; flex: 1; }
  .log-line {
    font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px;
    color: var(--neutral); line-height: 1.6; white-space: pre-wrap; word-break: break-all;
  }
  .log-line.error { color: var(--red); }
  .log-line.warn  { color: #F59E0B; }
  .log-line.info  { color: var(--neutral); }

  /* ── Footer ── */
  footer {
    border-top: 1px solid var(--line); padding: 16px 28px;
    display: flex; align-items: center; justify-content: space-between;
    color: var(--muted); font-size: 11px; max-width: 1280px; margin: 0 auto;
  }
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="logo-mark">P</div>
    <div>
      <div>POLYMARKET ALPHA</div>
      <div class="logo-sub">AUTOMATED TRADING SYSTEM</div>
    </div>
  </div>
  <div class="header-right">
    <span id="regime-pill" class="regime-pill regime-flat">RANGING</span>
    <span id="btc-price" style="font-size:12px;font-weight:600;color:var(--muted);font-variant-numeric:tabular-nums;"></span>
    <button class="btn" onclick="resetData()" style="font-size:10px;padding:4px 10px;color:var(--muted);">Reset Data</button>
    <div class="live-badge"><div class="live-dot"></div>LIVE</div>
    <span class="header-time" id="clock">--:--:--</span>
  </div>
</header>

<main>

  <!-- ── Account Overview ── -->
  <div class="hero">
    <div class="hero-cell">
      <div class="hero-label">On-chain USDC <span style="font-size:9px;color:var(--green);margin-left:4px;">&#9679; LIVE</span></div>
      <div class="hero-value" id="h-balance">--</div>
      <div class="hero-sub" id="h-balance-pnl">-- tracked P&amp;L</div>
    </div>
    <div class="hero-cell">
      <div class="hero-label">Liquid Cash</div>
      <div class="hero-value" id="h-liquid">--</div>
      <div class="hero-sub">Available to trade</div>
    </div>
    <div class="hero-cell">
      <div class="hero-label">Deployed</div>
      <div class="hero-value" id="h-deployed">--</div>
      <div class="hero-sub" id="h-open-count">-- open positions</div>
    </div>
    <div class="hero-cell">
      <div class="hero-label">Total P&amp;L</div>
      <div class="hero-value" id="h-pnl">--</div>
      <div class="hero-sub" id="h-pnl-pct">-- since inception</div>
    </div>
    <div class="hero-cell">
      <div class="hero-label">All-time Win Rate</div>
      <div class="hero-value" id="h-wr">--</div>
      <div class="hero-sub" id="h-trades">-- settled trades</div>
    </div>
  </div>

  <!-- ── Strategy Cards ── -->
  <div class="section-label">ACTIVE STRATEGIES</div>
  <div class="two-col">

    <!-- D2 -->
    <div class="strategy-card">
      <div class="card-header">
        <div class="card-title">
          <div class="strategy-icon icon-d2">D2</div>
          <div>
            <div style="font-size:13px;font-weight:600;">Directional Consensus</div>
            <div style="font-size:11px;color:var(--muted);font-weight:400;margin-top:2px;">S1–S10 · IC-weighted · S2 contrarian · S3 1.5× · BTC ETH DOGE</div>
          </div>
        </div>
        <span id="d2-status" class="status-badge status-off">OFFLINE</span>
      </div>
      <div class="card-body">
        <div class="stat-grid">
          <div class="stat-item">
            <div class="stat-label">P&amp;L</div>
            <div class="stat-value" id="d2-pnl">--</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value" id="d2-wr">--</div>
            <div class="stat-sub" id="d2-trades"></div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Open</div>
            <div class="stat-value" id="d2-open">--</div>
          </div>
        </div>
        <div class="divider"></div>
        <div class="last-trade">
          <span class="last-trade-label">Last trade</span>
          <span class="last-trade-val" id="d2-last">—</span>
        </div>
      </div>
      <div class="card-footer">
        <button class="btn" onclick="viewLog('d2')">View Log</button>
        <button class="btn btn-danger" id="d2-ctrl-btn" onclick="toggleBot('d2')">Stop</button>
      </div>
    </div>

    <!-- W -->
    <div class="strategy-card">
      <div class="card-header">
        <div class="card-title">
          <div class="strategy-icon icon-w">W</div>
          <div>
            <div style="font-size:13px;font-weight:600;">Whale Consensus</div>
            <div style="font-size:11px;color:var(--muted);font-weight:400;margin-top:2px;">On-chain copy trading · <span id="w-pool-size">--</span> wallets</div>
          </div>
        </div>
        <span id="w-status" class="status-badge status-off">OFFLINE</span>
      </div>
      <div class="card-body">
        <div class="stat-grid">
          <div class="stat-item">
            <div class="stat-label">P&amp;L</div>
            <div class="stat-value" id="w-pnl">--</div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Win Rate</div>
            <div class="stat-value" id="w-wr">--</div>
            <div class="stat-sub" id="w-trades"></div>
          </div>
          <div class="stat-item">
            <div class="stat-label">Open</div>
            <div class="stat-value" id="w-open">--</div>
          </div>
        </div>
        <div class="divider"></div>
        <div class="last-trade">
          <span class="last-trade-label">Last trade</span>
          <span class="last-trade-val" id="w-last">—</span>
        </div>
      </div>
      <div class="card-footer">
        <button class="btn" onclick="viewLog('w')">View Log</button>
        <button class="btn btn-danger" id="w-ctrl-btn" onclick="toggleBot('w')">Stop</button>
      </div>
    </div>

  </div>

  <!-- ── Balance History ── -->
  <div class="chart-section">
    <div class="chart-header">
      <div class="chart-title">Account Balance History</div>
      <div style="display:flex;gap:8px;align-items:center;">
        <div class="chart-tabs" id="bot-tabs">
          <div class="chart-tab active" onclick="setChartBot('all',this)">Combined</div>
          <div class="chart-tab" onclick="setChartBot('d2',this)">D2</div>
          <div class="chart-tab" onclick="setChartBot('w',this)">Whale</div>
        </div>
        <div style="width:1px;height:18px;background:var(--line1);"></div>
        <div class="chart-tabs" id="time-tabs">
          <div class="chart-tab" onclick="setChartTime(1,this)">1H</div>
          <div class="chart-tab" onclick="setChartTime(24,this)">24H</div>
          <div class="chart-tab" onclick="setChartTime(168,this)">7D</div>
          <div class="chart-tab" onclick="setChartTime(720,this)">30D</div>
          <div class="chart-tab active" onclick="setChartTime(0,this)">All</div>
        </div>
      </div>
    </div>
    <div class="chart-body">
      <canvas id="balanceChart"></canvas>
    </div>
  </div>

  <!-- ── Open Positions ── -->
  <div class="open-section">
    <div class="open-header">
      <div style="font-weight:600;font-size:13px;">Open Positions</div>
      <span id="open-count" class="count-badge">0</span>
    </div>
    <div id="open-body">
      <table class="open-table">
        <thead>
          <tr>
            <th>Bot</th>
            <th>Market</th>
            <th>Direction</th>
            <th>Entry</th>
            <th>Size</th>
            <th>Expires In</th>
          </tr>
        </thead>
        <tbody id="open-tbody"><tr><td colspan="6"><div class="empty-state">No open positions</div></td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- ── Trade History ── -->
  <div class="trades-section">
    <div class="trades-header">
      <div style="font-weight:600;font-size:13px;">Trade History</div>
      <div style="font-size:11px;color:var(--muted);">Last 50 settled</div>
    </div>
    <table class="trades-table">
      <thead>
        <tr>
          <th>Time</th>
          <th>Bot</th>
          <th>Market</th>
          <th>Direction</th>
          <th>Entry</th>
          <th>Exit</th>
          <th>Size</th>
          <th>P&amp;L</th>
          <th>Result</th>
        </tr>
      </thead>
      <tbody id="trades-tbody">
        <tr><td colspan="9"><div class="empty-state">Loading…</div></td></tr>
      </tbody>
    </table>
  </div>

</main>

<!-- ── Log Modal ── -->
<div class="modal-overlay" id="log-modal">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="log-title">Bot Log</div>
      <button class="modal-close" onclick="closeLog()">×</button>
    </div>
    <div class="modal-body" id="log-body"></div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
const botRunning = { d2: false, w: false };
let chartBot   = 'all';
let chartHours = 0;
let balChart   = null;
let openData   = [];

// ── Clock ──────────────────────────────────────────────────────────────────
function updateClock() {
  const now = new Date();
  document.getElementById('clock').textContent =
    now.toUTCString().slice(17, 25) + ' UTC';
}
setInterval(updateClock, 1000);
updateClock();

// ── Format helpers ─────────────────────────────────────────────────────────
function fmtUSD(v, alwaysSign = false) {
  const abs = Math.abs(v).toFixed(2);
  if (alwaysSign) return (v >= 0 ? '+$' : '-$') + abs;
  return '$' + abs;
}
function fmtPct(v) {
  return (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
}
function fmtAge(ts) {
  const s = Math.floor((Date.now() / 1000) - ts);
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s/60) + 'm ago';
  if (s < 86400) return Math.floor(s/3600) + 'h ago';
  return Math.floor(s/86400) + 'd ago';
}
function fmtTs(ts) {
  const d = new Date(ts < 1e12 ? ts * 1000 : ts);
  return d.toLocaleString('en-US', { month:'short', day:'numeric',
    hour:'2-digit', minute:'2-digit', hour12: false, timeZone:'UTC' }) + ' UTC';
}
function colorClass(v, el) {
  el.className = el.className.replace(/\bpos\b|\bneg\b|\bgold\b/g, '').trim();
  if (v > 0) el.classList.add('pos');
  else if (v < 0) el.classList.add('neg');
}
function setEl(id, text) { const e = document.getElementById(id); if(e) e.textContent = text; }
function setHTML(id, html) { const e = document.getElementById(id); if(e) e.innerHTML = html; }

// ── Status load ────────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const r   = await fetch('/api/status');
    const data = await r.json();
    const br   = data.bankroll;
    const d2   = data.bots.d2;
    const w    = data.bots.w;

    // Hero — balances
    const bal = data.balances;
    const balEl = document.getElementById('h-balance');
    // Primary balance: live on-chain USDC (authoritative); fall back to computed if unavailable
    const liveUsdc = data.live_usdc;
    balEl.textContent = liveUsdc != null ? fmtUSD(liveUsdc) : fmtUSD(bal.total);
    const trackedPnl = bal.realized_pnl;
    const balPnlEl = document.getElementById('h-balance-pnl');
    balPnlEl.textContent = fmtUSD(trackedPnl, true) + ' tracked P\u0026L';
    colorClass(trackedPnl, balPnlEl);

    const liqEl = document.getElementById('h-liquid');
    // Liquid = on-chain balance minus deployed
    const liveBalance = liveUsdc != null ? liveUsdc : bal.total;
    liqEl.textContent = fmtUSD(Math.max(0, liveBalance - bal.deployed));

    const depEl = document.getElementById('h-deployed');
    depEl.textContent = fmtUSD(bal.deployed);

    const totalOpen = d2.open + w.open;
    setEl('h-open-count', totalOpen + ' open position' + (totalOpen !== 1 ? 's' : ''));

    const pnlEl = document.getElementById('h-pnl');
    const pnl = data.combined_pnl;
    pnlEl.textContent = fmtUSD(pnl, true);
    colorClass(pnl, pnlEl);

    const totalGain = liveBalance - bal.initial;
    const pct = bal.initial > 0 ? (totalGain / bal.initial * 100) : 0;
    setEl('h-pnl-pct', fmtPct(pct) + ' since inception');

    const totalTrades = d2.total_closed + w.total_closed;
    const totalWins   = d2.wins + w.wins;
    const wr = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;
    const wrEl = document.getElementById('h-wr');
    wrEl.textContent = wr.toFixed(1) + '%';
    colorClass(wr >= 52 ? 1 : -1, wrEl);
    setEl('h-trades', totalTrades + ' settled trades');

    // D2 card
    updateBotCard('d2', d2);
    // W card
    updateBotCard('w', w);
    if (w.whale_pool_size !== undefined)
      setEl('w-pool-size', w.whale_pool_size + ' wallets');

    // Regime
    const regime    = data.regime;
    const regPct    = data.regime_pct;
    const pillEl    = document.getElementById('regime-pill');
    const priceEl   = document.getElementById('btc-price');
    pillEl.className = 'regime-pill ' +
      (regime === 'Up' ? 'regime-up' : regime === 'Down' ? 'regime-down' : 'regime-flat');
    pillEl.textContent = 'BTC ' + (regime === 'Up' ? '▲' : regime === 'Down' ? '▼' : '—') +
      (regPct != null ? ' ' + fmtPct(regPct) + ' 4H' : '');
    if (data.btc_price)
      priceEl.textContent = '$' + Number(data.btc_price).toLocaleString();

  } catch(e) { console.error('status error', e); }
}

function updateBotCard(bot, stats) {
  const running = stats.running;
  const statusEl = document.getElementById(bot + '-status');
  statusEl.textContent = running ? 'LIVE' : 'OFFLINE';
  statusEl.className   = 'status-badge ' + (running ? 'status-live' : 'status-off');

  const pnlEl = document.getElementById(bot + '-pnl');
  pnlEl.textContent = fmtUSD(stats.pnl, true);
  colorClass(stats.pnl, pnlEl);

  setEl(bot + '-wr', stats.wr + '%');
  setEl(bot + '-trades', stats.wins + 'W / ' + stats.losses + 'L');
  setEl(bot + '-open', stats.open);

  botRunning[bot] = running;
  const ctrlBtn = document.getElementById(bot + '-ctrl-btn');
  if (ctrlBtn) {
    ctrlBtn.textContent = running ? 'Stop' : 'Start';
    ctrlBtn.className   = running ? 'btn btn-danger' : 'btn btn-start';
  }

  const lt = stats.last_trade;
  if (lt) {
    const sym = lt.symbol || lt.asset || '?';
    const dir = lt.direction || '?';
    const pnl = Number(lt.realized_pnl || 0);
    const age = lt.ts_close ? fmtAge(lt.ts_close < 1e12 ? lt.ts_close : lt.ts_close/1000) : '?';
    const sign = pnl >= 0 ? '+' : '';
    setHTML(bot + '-last',
      `<span style="color:var(--muted)">${sym} ${dir} — </span>` +
      `<span class="${pnl >= 0 ? 'pos' : 'neg'}">${sign}$${Math.abs(pnl).toFixed(2)}</span>` +
      `<span style="color:var(--muted)"> · ${age}</span>`
    );
  } else {
    setEl(bot + '-last', '—');
  }
}

// ── Open positions ─────────────────────────────────────────────────────────
async function loadOpenPositions() {
  try {
    const r    = await fetch('/api/open_positions');
    const data = await r.json();
    openData   = data;
    setEl('open-count', data.length);

    const tbody = document.getElementById('open-tbody');
    if (!data.length) {
      tbody.innerHTML = '<tr><td colspan="6"><div class="empty-state">No open positions</div></td></tr>';
      return;
    }
    const now = Date.now() / 1000;
    tbody.innerHTML = data.map(p => {
      const endTime = p.end_time || 0;
      const secsLeft = Math.max(0, Math.floor(endTime - now));
      const mins = Math.floor(secsLeft/60), secs = secsLeft%60;
      const ageStr = secsLeft <= 0 ? 'expired' : (mins > 0 ? mins + 'm ' + secs + 's' : secs + 's');
      const bot    = p._bot || '?';
      const dir    = p.direction || '?';
      const sym    = (p.symbol || '?').replace('-USD','');
      return `<tr>
        <td><span class="bot-tag bot-${bot.toLowerCase()}">${bot}</span></td>
        <td>${sym}</td>
        <td class="${dir === 'Up' ? 'dir-up' : 'dir-down'}">${dir === 'Up' ? '▲ Up' : '▼ Down'}</td>
        <td>${Number(p.entry_price||0).toFixed(3)}</td>
        <td>$${Number(p.cost_usd||0).toFixed(2)}</td>
        <td>${ageStr}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('open positions error', e); }
}

// ── Trade history ──────────────────────────────────────────────────────────
async function loadTrades() {
  try {
    const r    = await fetch('/api/trades');
    const data = await r.json();
    const tbody = document.getElementById('trades-tbody');
    if (!data.length) {
      tbody.innerHTML = '<tr><td colspan="9"><div class="empty-state">No settled trades yet</div></td></tr>';
      return;
    }
    tbody.innerHTML = data.slice(0, 50).map(t => {
      const ts     = t.ts_close || 0;
      const won    = t.status === 'won';
      const pnl    = Number(t.realized_pnl || 0);
      const bot    = t._bot || '?';
      const dir    = t.direction || '?';
      const sym    = (t.symbol || '?').replace('-USD','');
      const entry  = Number(t.entry_price || 0).toFixed(3);
      const exit   = Number(t.exit_price  || 0).toFixed(3);
      const cost   = Number(t.cost_usd    || 0).toFixed(2);
      const pnlStr = (pnl >= 0 ? '+' : '') + '$' + Math.abs(pnl).toFixed(2);
      const timeStr = ts ? fmtTs(ts < 1e12 ? ts * 1000 : ts) : '—';
      return `<tr>
        <td style="color:var(--muted)">${timeStr}</td>
        <td><span class="bot-tag bot-${bot.toLowerCase()}">${bot}</span></td>
        <td>${sym}</td>
        <td class="${dir === 'Up' ? 'dir-up' : 'dir-down'}">${dir === 'Up' ? '▲ Up' : '▼ Down'}</td>
        <td>${entry}</td>
        <td>${exit}</td>
        <td>$${cost}</td>
        <td class="${pnl >= 0 ? 'pos' : 'neg'}">${pnlStr}</td>
        <td><span class="outcome-pill ${won ? 'outcome-won' : 'outcome-lost'}">${won ? 'WIN' : 'LOSS'}</span></td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('trades error', e); }
}

// ── Balance chart ──────────────────────────────────────────────────────────
async function loadChart(bot, hours) {
  if (hours === undefined) hours = chartHours;
  try {
    const r    = await fetch('/api/balance_history?bot=' + bot + '&hours=' + hours);
    const data = await r.json();

    const labels = data.map(p => {
      const d = new Date(p[0]);
      return d.toLocaleString('en-US', { month:'short', day:'numeric',
        hour:'2-digit', minute:'2-digit', hour12: false, timeZone:'America/Los_Angeles' });
    });
    const values = data.map(p => p[1]);

    const ctx  = document.getElementById('balanceChart').getContext('2d');
    const last = values.length ? values[values.length-1] : 97.28;
    const init = values.length ? values[0] : 97.28;
    const isUp = last >= init;
    const color = isUp ? '#10B981' : '#EF4444';
    const colorDim = isUp ? 'rgba(16,185,129,.12)' : 'rgba(239,68,68,.12)';

    if (balChart) balChart.destroy();

    balChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          data: values,
          borderColor: color,
          borderWidth: 2,
          fill: true,
          backgroundColor: (ctx2) => {
            const gradient = ctx2.chart.ctx.createLinearGradient(0, 0, 0, 220);
            gradient.addColorStop(0, colorDim);
            gradient.addColorStop(1, 'transparent');
            return gradient;
          },
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: color,
          tension: 0.3,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: '#0D111A',
            borderColor: '#1C2233',
            borderWidth: 1,
            titleColor: '#4A5568',
            bodyColor: '#E2E8F3',
            titleFont: { size: 10, weight: '600', family: 'Inter' },
            bodyFont: { size: 12, weight: '700', family: 'Inter' },
            callbacks: {
              label: ctx2 => ' $' + ctx2.raw.toFixed(2)
            }
          }
        },
        scales: {
          x: {
            grid: { color: 'rgba(28,34,51,.6)', drawBorder: false },
            ticks: { color: '#4A5568', font: { size: 10, family: 'Inter' }, maxTicksLimit: 8,
              maxRotation: 0 },
            border: { color: '#1C2233' }
          },
          y: {
            position: 'right',
            grid: { color: 'rgba(28,34,51,.6)', drawBorder: false },
            ticks: { color: '#4A5568', font: { size: 10, family: 'Inter' },
              callback: v => '$' + v.toFixed(0) },
            border: { color: '#1C2233' }
          }
        }
      }
    });
  } catch(e) { console.error('chart error', e); }
}

function setChartBot(bot, el) {
  chartBot = bot;
  document.querySelectorAll('#bot-tabs .chart-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  loadChart(chartBot, chartHours);
}

function setChartTime(hours, el) {
  chartHours = hours;
  document.querySelectorAll('#time-tabs .chart-tab').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  loadChart(chartBot, chartHours);
}

// ── Bot controls ───────────────────────────────────────────────────────────
async function toggleBot(bot) {
  const action = botRunning[bot] ? 'stop' : 'start';
  const btn = document.getElementById(bot + '-ctrl-btn');
  btn.disabled = true;
  try {
    await fetch('/api/bot/' + bot + '/' + action, { method: 'POST' });
    await loadStatus();
  } catch(e) { console.error(e); }
  btn.disabled = false;
}

async function resetData() {
  if (!confirm('Reset all trade data and bankroll to $97.28?')) return;
  try {
    await fetch('/api/reset', { method: 'POST' });
    await refreshAll();
    loadChart(chartBot, chartHours);
  } catch(e) { console.error(e); }
}

// ── Log modal ──────────────────────────────────────────────────────────────
async function viewLog(bot) {
  document.getElementById('log-modal').classList.add('open');
  document.getElementById('log-title').textContent = bot.toUpperCase() + ' — Live Log';
  const body = document.getElementById('log-body');
  body.innerHTML = '<div class="log-line">Loading…</div>';
  try {
    const r    = await fetch('/api/log/' + bot);
    const data = await r.json();
    body.innerHTML = data.lines.map(l => {
      const cls = l.includes('[ERROR]') ? 'error' : l.includes('[WARN]') ? 'warn' : 'info';
      return `<div class="log-line ${cls}">${escapeHTML(l)}</div>`;
    }).join('');
    body.scrollTop = body.scrollHeight;
  } catch(e) { body.innerHTML = '<div class="log-line error">Failed to load log</div>'; }
}
function closeLog() { document.getElementById('log-modal').classList.remove('open'); }
document.getElementById('log-modal').addEventListener('click', e => {
  if (e.target === e.currentTarget) closeLog();
});
function escapeHTML(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Init & refresh ─────────────────────────────────────────────────────────
async function refreshAll() {
  await Promise.all([loadStatus(), loadOpenPositions(), loadTrades()]);
}

refreshAll();
loadChart(chartBot);

setInterval(refreshAll, 15000);
setInterval(() => loadChart(chartBot, chartHours), 60000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML
