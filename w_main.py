"""
w_main.py — Whalebot main loop

Single unified production whale copy-trading bot.
Replaces C1-C4 with a single CCC (Collective Confidence Consensus) approach.

Signal flow:
  1. Polygon WebSocket → WhaleTracker detects whale buy → fires WhaleTrade
  2. CCCEngine records event in rolling 120s activity window
  3. CCCEngine.compute() → weighted majority score for condition_id + direction
  4. ThresholdEngine.compute() → dynamic threshold from S1-S6 signals
  5. If ccc_score > threshold → execute via PolyExecutor
  6. PolyExecutor uses shared bankroll + Kelly sizing

Hot-reload loops (no restarts needed):
  - ccc_reload_loop():        every 30 min — reloads wallet_calibration.json
  - whale_pool_reload_loop(): every 15 min — reloads whale_pool.json

Run:
    set -a && source .env && set +a
    nohup python3 w_main.py >> logs_w/bot.log 2>&1 &
"""
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Config ───────────────────────────────────────────────────────────────────
BOT_LABEL       = os.getenv("BOT_LABEL",    "W")
BANKROLL_USD    = float(os.getenv("BANKROLL_USD",  "107"))
LOGS_DIR        = os.getenv("LOGS_DIR",     "logs_w")
PAPER_TRADING   = os.getenv("PAPER_TRADING", "1") != "0"
DIAG_INTERVAL   = int(os.getenv("DIAG_INTERVAL", "60"))
MIN_WHALE_CONF  = float(os.getenv("MIN_WHALE_CONF", "0.60"))
WHALE_CONSENSUS_WINDOW_SECS = float(os.getenv("WHALE_CONSENSUS_WINDOW_SECS", "120"))
CONF_BASE       = float(os.getenv("CONF_BASE", "0.55"))
CONF_MAX        = float(os.getenv("CONF_MAX",  "0.82"))
WHALE_MIN_SECS   = int(os.getenv("WHALE_MIN_SECS", "30"))
CLOB_VETO_RATIO  = float(os.getenv("CLOB_VETO_RATIO", "2.0"))
MIN_LIQUIDITY_USD = float(os.getenv("POLY_MIN_LIQUIDITY", "500"))

# ── Logging ──────────────────────────────────────────────────────────────────
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(f"{LOGS_DIR}/bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("w_main")

for lib in ("httpx", "websockets", "hpack", "h2"):
    logging.getLogger(lib).setLevel(logging.WARNING)

# ── Imports ───────────────────────────────────────────────────────────────────
from market_finder   import MarketFinder
from whale_tracker   import WhaleTracker, WhaleTrade, WHALE_ADDRESSES, WHALE_CONFIDENCE
from poly_executor   import PolyExecutor
from telegram_alerts import TelegramAlerts
from ccc_engine      import CCCEngine
from threshold_engine import ThresholdEngine
from quality_model   import QualityModel
from choppiness_gate import ChoppinessGate
import polymarket_data

# ── CCC calibration startup load ─────────────────────────────────────────────
_CCC_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wallet_calibration.json")

def _reload_ccc():
    """Re-read wallet_calibration.json and update WHALE_CONFIDENCE in-place."""
    if not os.path.exists(_CCC_PATH):
        return
    try:
        calib = json.load(open(_CCC_PATH))
        name_to_addr = {v: k for k, v in WHALE_ADDRESSES.items()}
        applied = 0
        for name, conf in calib.items():
            addr = name_to_addr.get(name)
            if addr and addr in WHALE_CONFIDENCE:
                if conf >= MIN_WHALE_CONF:
                    WHALE_CONFIDENCE[addr] = conf
                    applied += 1
                else:
                    WHALE_CONFIDENCE[addr] = 0.0   # mark below threshold
        logger.info(f"CCC reload: applied {applied}/{len(calib)} from {_CCC_PATH}")
    except Exception as e:
        logger.warning(f"CCC reload failed: {e}")

_reload_ccc()

# ── Whale pool hot-reload ─────────────────────────────────────────────────────
_POOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whale_pool.json")

def _reload_whale_pool():
    """Read whale_pool.json and update WHALE_ADDRESSES / WHALE_CONFIDENCE."""
    if not os.path.exists(_POOL_PATH):
        return
    try:
        pool = json.load(open(_POOL_PATH))
        added = evicted = 0
        for addr, meta in pool.items():
            name = meta.get("name", addr[:12])
            conf = float(meta.get("conf", 0.55))
            in_pool = meta.get("in_pool", True)
            if in_pool and conf >= MIN_WHALE_CONF:
                if addr not in WHALE_ADDRESSES:
                    WHALE_ADDRESSES[addr] = name
                    added += 1
                WHALE_CONFIDENCE[addr] = conf
            elif not in_pool and addr in WHALE_ADDRESSES:
                del WHALE_ADDRESSES[addr]
                WHALE_CONFIDENCE.pop(addr, None)
                evicted += 1
        if added or evicted:
            logger.info(f"Whale pool reload: +{added} / -{evicted} wallets "
                        f"(pool size now {len(WHALE_ADDRESSES)})")
    except Exception as e:
        logger.warning(f"Whale pool reload failed: {e}")

_reload_whale_pool()   # apply at startup too

# ── Session state ─────────────────────────────────────────────────────────────
_shutdown  = asyncio.Event()
_entered:  set[str] = set()      # "condition_id-direction" already entered this session
SIGNAL_LOG = f"{LOGS_DIR}/signals.jsonl"


def _log_signal(rec: dict):
    try:
        with open(SIGNAL_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as e:
        logger.warning(f"signal log write failed: {e}")


# ── Confidence mapping from CCC score ─────────────────────────────────────────

def _ccc_to_confidence(ccc_score: float) -> float:
    """
    Map CCC score [0, 1] to Kelly confidence [CONF_BASE, CONF_MAX].
    A score of 0.60 → 0.656; a score of 0.95 → 0.801.
    """
    return round(CONF_BASE + (CONF_MAX - CONF_BASE) * ccc_score, 4)


# ── Whale event handler ───────────────────────────────────────────────────────

async def on_whale_trade(
    signal:           WhaleTrade,
    executor:         PolyExecutor,
    market_finder:    MarketFinder,
    ccc_engine:       CCCEngine,
    threshold_engine: ThresholdEngine,
    tg:               TelegramAlerts,
    http_client:      httpx.AsyncClient,
    quality_model:    QualityModel,
    chop_gate:        ChoppinessGate,
):
    # 1. Record event in activity window
    ccc_conf = WHALE_CONFIDENCE.get(signal.whale_addr, 0.0)
    if ccc_conf < MIN_WHALE_CONF:
        logger.debug(
            f"on_whale_trade: {signal.whale_name} conf={ccc_conf:.2f} < "
            f"MIN_WHALE_CONF={MIN_WHALE_CONF}, skip"
        )
        return

    ccc_engine.record_event(
        condition_id = signal.condition_id,
        addr         = signal.whale_addr,
        name         = signal.whale_name,
        direction    = signal.direction,
        ccc_conf     = ccc_conf,
    )

    # 2. Compute CCC score
    ccc = ccc_engine.compute(signal.condition_id, signal.direction)

    # 3. Compute dynamic threshold (S1-S6)
    asset = signal.symbol.split("-")[0]      # "BTC-USD" → "BTC"
    ts_ms = int(signal.ts * 1000)
    token_id = signal.token_id

    threshold, sig_adjustments = await threshold_engine.compute(
        client    = http_client,
        asset     = asset,
        direction = signal.direction,
        token_id  = token_id,
        ts_ms     = ts_ms,
    )

    # 4. Build signal record (always logged)
    seconds_left = signal.end_time - time.time()
    consensus_conf = _ccc_to_confidence(ccc.ccc_score)

    sig_rec = {
        "ts":                int(signal.ts * 1000),
        "asset":             asset,
        "direction":         signal.direction,
        "condition_id":      signal.condition_id,
        "token_id":          token_id,
        "symbol":            signal.symbol,
        "secs_left":         round(seconds_left, 1),
        "ccc_score":         ccc.ccc_score,
        "threshold":         threshold,
        "n_active_whales":   ccc.n_active,
        "agreeing_weight":   ccc.agreeing_weight,
        "total_active_weight": ccc.total_weight,
        "whale_details":     ccc.whale_details,
        "signal_adjustments": sig_adjustments,
        "consensus_confidence": consensus_conf,
        "bot":               BOT_LABEL,
        "executed":          False,
    }

    # 5. Gate check
    if ccc.ccc_score <= threshold:
        logger.debug(
            f"[{BOT_LABEL}] {asset} {signal.direction} | "
            f"ccc={ccc.ccc_score:.3f} <= thr={threshold:.3f} n={ccc.n_active} — wait"
        )
        _log_signal(sig_rec)
        return

    cond_key = f"{signal.condition_id}-{signal.direction}"
    if cond_key in _entered:
        logger.debug(f"[{BOT_LABEL}] {cond_key} already entered, skip")
        return

    if seconds_left < WHALE_MIN_SECS:
        logger.debug(f"[{BOT_LABEL}] only {seconds_left:.0f}s left, skip")
        _log_signal({**sig_rec, "skip_reason": "too_close_to_expiry"})
        return

    # Choppiness gate (global)
    if chop_gate.is_paused():
        logger.info(
            f"[{BOT_LABEL}] {asset} {signal.direction} — "
            f"market choppy, skip"
        )
        _log_signal({**sig_rec, "skip_reason": "choppy_market"})
        return

    # Quality gate (per entity)
    entity_key = f"{signal.whale_name.lower()}_{signal.direction.lower()}"
    kelly_mult = quality_model.get_multiplier(entity_key)
    if kelly_mult == 0.0:
        logger.info(
            f"[{BOT_LABEL}] {asset} {signal.direction} — "
            f"{entity_key} below quality floor, skip"
        )
        _log_signal({**sig_rec, "skip_reason": "quality_floor"})
        return

    # 6. Execute
    market = market_finder.get_market_by_condition(signal.condition_id)
    if not market:
        return

    # Liquidity gate
    if market.get("liquidity", 0.0) < MIN_LIQUIDITY_USD:
        logger.info(f"[{BOT_LABEL}] {asset} {signal.direction} — illiquid market, skip")
        _log_signal({**sig_rec, "skip_reason": "illiquid_market"})
        return

    # CLOB gate
    token_id = market["up_token_id"] if signal.direction == "Up" else market["down_token_id"]
    bid_usd, ask_usd = await polymarket_data.get_orderbook(token_id)
    if bid_usd > 0 and ask_usd > 0 and (ask_usd / bid_usd) > CLOB_VETO_RATIO:
        logger.info(f"[CLOB] skip {asset} {signal.direction} — ask/bid={ask_usd/bid_usd:.2f} "
                    f"(ask=${ask_usd:.0f} bid=${bid_usd:.0f})")
        _log_signal({**sig_rec, "skip_reason": "clob_imbalance"})
        return

    pos = await executor.execute(
        market           = market,
        direction        = signal.direction,
        confidence       = consensus_conf,
        source           = f"whale:{signal.whale_name}",
        kelly_multiplier = kelly_mult,
    )

    if pos:
        _entered.add(cond_key)
        contracts = pos.contracts
        entry_px  = pos.entry_price
        cost      = pos.cost_usd

        sig_rec.update({
            "executed":    True,
            "contracts":   contracts,
            "entry_price": entry_px,
            "cost_usd":    cost,
        })
        _log_signal(sig_rec)

        msg = (
            f"[{BOT_LABEL}] {asset} {signal.direction} | "
            f"ccc={ccc.ccc_score:.3f} thr={threshold:.3f} n={ccc.n_active} "
            f"@{entry_px:.3f} ${cost:.2f}"
        )
        logger.info(f"Entered: {msg}")
        try:
            await tg.send(msg)
        except Exception:
            pass
    else:
        _log_signal({**sig_rec, "skip_reason": "executor_skip"})


# ── Loops ─────────────────────────────────────────────────────────────────────

async def settlement_loop(executor: PolyExecutor, tg: TelegramAlerts, quality_model: QualityModel):
    while not _shutdown.is_set():
        try:
            open_before = {p.id for p in executor.open_positions()}
            await executor.check_and_settle_expired()
            open_after  = {p.id for p in executor.open_positions()}

            settled = open_before - open_after
            if settled:
                closed_map = {p.id: p for p in executor.closed_positions()}
                for pos_id in settled:
                    pos = closed_map.get(pos_id)
                    if not pos:
                        continue
                    won = pos.status == "won"
                    whale_name = None
                    src = pos.source or ""
                    if ":" in src:
                        whale_name = src.split(":", 1)[1].strip() or None
                    if whale_name:
                        entity_key = f"{whale_name.lower()}_{pos.direction.lower()}"
                        quality_model.record(entity_key, won)
                    pnl     = pos.realized_pnl or 0.0
                    icon    = "✅" if won else "❌"
                    outcome = "WON" if won else "LOST"
                    pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                    s_w = executor.summary()
                    bal_after_w  = s_w["bankroll"]
                    bal_before_w = round(bal_after_w - pnl, 2)
                    msg = (
                        "[" + BOT_LABEL + "] " + icon + " " + pos.symbol
                        + " " + pos.direction + " " + outcome + " " + pnl_str
                        + " (" + f"{pos.entry_price:.3f}" + "\u2192" + f"{pos.exit_price:.3f}" + ")"
                        + "\nBankroll: $" + f"{bal_before_w:.2f}" + "\u2192$" + f"{bal_after_w:.2f}"
                    )
                    logger.info("Settled: " + msg)
                    try:
                        await tg.send(msg)
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"settlement_loop: {e}", exc_info=True)
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def ccc_reload_loop():
    """Reload wallet_calibration.json every 30 minutes."""
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=1800)
        except asyncio.TimeoutError:
            pass
        else:
            break
        _reload_ccc()


async def whale_pool_reload_loop():
    """Reload whale_pool.json every 15 minutes."""
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=900)
        except asyncio.TimeoutError:
            pass
        else:
            break
        _reload_whale_pool()


async def cleanup_loop(ccc_engine: CCCEngine):
    """Evict stale condition_ids from the CCC activity window every hour."""
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=3600)
        except asyncio.TimeoutError:
            pass
        else:
            break
        ccc_engine.cleanup_old_markets()


async def market_refresh_loop(mf: MarketFinder):
    try:
        await mf.run_refresh_loop()
    except Exception as e:
        logger.error(f"market_refresh_loop crashed: {e}", exc_info=True)


async def diag_loop(whale_tracker: WhaleTracker, executor: PolyExecutor,
                    market_finder: MarketFinder):
    while not _shutdown.is_set():
        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=DIAG_INTERVAL)
        except asyncio.TimeoutError:
            pass
        else:
            break
        s = executor.summary()
        logger.info(
            f"DIAG [{BOT_LABEL}] mode={s['mode']} "
            f"poly_events={whale_tracker.events_received} "
            f"whale_hits={whale_tracker.whale_hits} loser_hits={whale_tracker.loser_hits} "
            f"sigs_fired={whale_tracker.signals_fired} "
            f"open={s['open']} closed={s['closed']} pnl=${s['total_pnl']:+.2f} "
            f"balance=${s['bankroll']:.2f} "
            f"markets={market_finder.markets_loaded} "
            f"pool_size={len(WHALE_ADDRESSES)}"
        )


# ── Telegram commands ─────────────────────────────────────────────────────────

def _register_commands(tg: TelegramAlerts, executor: PolyExecutor,
                       whale_tracker: WhaleTracker, mf: MarketFinder,
                       ccc_engine: CCCEngine):

    async def cmd_status():
        s = executor.summary()
        return (
            f"*[{BOT_LABEL}]* mode={s['mode']}\n"
            f"open={s['open']} closed={s['closed']} "
            f"W:{s['won']} L:{s['lost']} WR:{s['win_rate']}%\n"
            f"pnl=${s['total_pnl']:+.2f} balance=${s['bankroll']:.2f}\n"
            f"poly_events={whale_tracker.events_received} "
            f"whale_hits={whale_tracker.whale_hits}\n"
            f"pool={len(WHALE_ADDRESSES)} markets={mf.markets_loaded}"
        )

    async def cmd_positions():
        positions = executor.open_positions()
        if not positions:
            return "No open positions."
        lines = ["*OPEN POSITIONS*"]
        for p in positions[:15]:
            secs = p.end_time - time.time()
            lines.append(
                f"{p.direction} {p.symbol[:3]} {p.contracts}c "
                f"@{p.entry_price:.3f} cost=${p.cost_usd:.2f} "
                f"src={p.source[:12]} exp={max(secs,0):.0f}s"
            )
        return "\n".join(lines)

    async def cmd_pnl():
        s = executor.summary()
        return (
            f"*[{BOT_LABEL}] P&L*\n"
            f"Trades: {s['closed']} (W:{s['won']} L:{s['lost']} WR:{s['win_rate']}%)\n"
            f"Total: ${s['total_pnl']:+.2f}  Balance: ${s['bankroll']:.2f}"
        )

    async def cmd_whales():
        lines = ["*TRACKED WHALES*"]
        for addr, name in list(WHALE_ADDRESSES.items())[:20]:
            conf = WHALE_CONFIDENCE.get(addr, 0.0)
            status = "" if conf >= MIN_WHALE_CONF else " [BELOW_THRESH]"
            lines.append(f"  {name}  ccc={conf:.2f}{status}  {addr[:12]}…")
        lines.append(f"Total: {len(WHALE_ADDRESSES)}")
        return "\n".join(lines)

    async def cmd_pause():
        executor.paper_mode = True
        return f"[{BOT_LABEL}] Trading PAUSED — paper mode forced on."

    async def cmd_resume():
        executor.paper_mode = PAPER_TRADING
        return f"[{BOT_LABEL}] Trading RESUMED [{'PAPER' if PAPER_TRADING else 'LIVE'}]."

    tg.register_command("/status",    cmd_status)
    tg.register_command("/positions", cmd_positions)
    tg.register_command("/pnl",       cmd_pnl)
    tg.register_command("/whales",    cmd_whales)
    tg.register_command("/pause",     cmd_pause)
    tg.register_command("/resume",    cmd_resume)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    mode_str = "PAPER" if PAPER_TRADING else "LIVE"
    print("=" * 65)
    print(f"  POLYMARKET WHALEBOT [{BOT_LABEL}]  —  {mode_str}")
    print(f"  BANKROLL              : ${BANKROLL_USD:.2f}")
    print(f"  MIN_WHALE_CONF        : {MIN_WHALE_CONF}")
    print(f"  CONSENSUS_WINDOW_SECS : {WHALE_CONSENSUS_WINDOW_SECS}")
    print(f"  POOL_SIZE             : {len(WHALE_ADDRESSES)}")
    print("=" * 65)

    tg           = TelegramAlerts()
    mf           = MarketFinder()
    executor     = PolyExecutor(bankroll_usd=BANKROLL_USD, paper_mode=PAPER_TRADING)
    quality_model = QualityModel(
        positions_path = f"{LOGS_DIR}/positions.jsonl",
        persist_path   = "quality_model.json",
    )
    quality_model.warmup(
        entity_key_fn=lambda p: (
            f"{p['source'].split(':')[1].lower()}_{p['direction'].lower()}"
            if ":" in p.get("source", "")
            else None
        )
    )
    chop_gate = ChoppinessGate()
    ccc_engine   = CCCEngine(window_secs=WHALE_CONSENSUS_WINDOW_SECS, min_conf=MIN_WHALE_CONF)
    thresh_engine = ThresholdEngine()

    # Single shared HTTP client for threshold signals
    http_client = httpx.AsyncClient(timeout=15.0)

    async def on_whale(sig: WhaleTrade):
        await on_whale_trade(
            signal           = sig,
            executor         = executor,
            market_finder    = mf,
            ccc_engine       = ccc_engine,
            threshold_engine = thresh_engine,
            tg               = tg,
            http_client      = http_client,
            quality_model    = quality_model,
            chop_gate        = chop_gate,
        )

    whale_tracker = WhaleTracker(market_finder=mf, on_whale_trade=on_whale)
    _register_commands(tg, executor, whale_tracker, mf, ccc_engine)

    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown.set)
        except (NotImplementedError, AttributeError):
            pass

    logger.info("Loading active markets…")
    await mf.refresh()
    logger.info(f"Loaded {mf.markets_loaded} Up-or-Down markets")

    await tg.send(
        f"[{BOT_LABEL}] WHALEBOT STARTED ({mode_str})\n"
        f"Pool: {len(WHALE_ADDRESSES)} wallets | "
        f"Markets: {mf.markets_loaded} | "
        f"Window: {WHALE_CONSENSUS_WINDOW_SECS}s"
    )

    _chop_task = asyncio.create_task(chop_gate.update_loop())
    try:
        await asyncio.gather(
            whale_tracker.run(),
            market_refresh_loop(mf),
            settlement_loop(executor, tg, quality_model),
            ccc_reload_loop(),
            whale_pool_reload_loop(),
            cleanup_loop(ccc_engine),
            diag_loop(whale_tracker, executor, mf),
            tg.poll_commands(),
        )
    finally:
        _chop_task.cancel()
        try:
            await _chop_task
        except asyncio.CancelledError:
            pass

    logger.info(f"[{BOT_LABEL}] Shutting down…")
    s = executor.summary()
    try:
        await tg.send(
            f"[{BOT_LABEL}] SHUTDOWN\n"
            f"Trades: {s['closed']} (W:{s['won']} L:{s['lost']})\n"
            f"P&L: ${s['total_pnl']:+.2f}"
        )
    except Exception:
        pass
    whale_tracker.stop()
    await http_client.aclose()
    await executor.close()
    await mf.close()
    await polymarket_data.close()
    await tg.close()


if __name__ == "__main__":
    asyncio.run(main())
