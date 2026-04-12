"""
drift_ev.py — Latency arbitrage / mispricing detection for Polymarket Up-or-Down markets.

Core insight: In the last N minutes of a market's resolution window, the current
asset price drift vs the reference price (market open) is highly predictive of the
outcome. If the CLOB hasn't repriced to match, there is exploitable EV.

Formula (Brownian bridge):
    P(win) = Φ( drift / σ(τ) )

    where:
        drift    = (current_price - ref_price) / ref_price   (sign-adjusted for direction)
        σ(τ)     = daily_vol × √(τ / 86400)                 (vol over remaining time)
        Φ        = standard normal CDF

    EV = P(win) × PAYOUT_RATE − ask_price

Usage:
    result = await compute_ev(asset, direction, token_id, condition_id,
                              end_time, market_duration_secs, client)
    if result.has_edge:
        # enter trade, use result.ev to boost confidence
"""

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# ── tunables (all env-overridable) ───────────────────────────────────────────
MIN_EV_THRESHOLD  = float(os.getenv("MIN_EV_THRESHOLD",  "0.04"))   # min EV to enter
EV_WINDOW_TIMEOUT = int(os.getenv("EV_WINDOW_TIMEOUT",   "90"))     # max secs to wait for window
EV_POLL_INTERVAL  = int(os.getenv("EV_POLL_INTERVAL",    "5"))      # poll cadence in seconds
EV_CONF_BOOST     = float(os.getenv("EV_CONF_BOOST",     "0.5"))    # conf += ev * boost
PAYOUT_RATE       = 0.99                                             # 1% taker fee

# ── Binance proxy (Frankfurt droplet) — fallback price source ────────────────
_BINANCE_PROXY = os.getenv("BINANCE_PROXY", "http://138.197.181.139:8081")
_PROXY_TOKEN   = os.getenv("PROXY_TOKEN",   "poly_binance_proxy_2026")
_BN_SYMBOL     = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "DOGE": "DOGEUSDT", "XRP": "XRPUSDT"}

# Fallback per-asset daily volatility (fraction; overridden by live compute)
ASSET_VOL_DAILY: Dict[str, float] = {
    "BTC":  0.022,
    "ETH":  0.026,
    "DOGE": 0.042,
    "XRP":  0.032,
    "SOL":  0.036,
}

# ── result dataclass ─────────────────────────────────────────────────────────
@dataclass
class EVResult:
    ev:              float   # expected value per contract (e.g. 0.08 = 8¢ edge)
    p_win:           float   # Brownian bridge P(win)
    ask:             float   # CLOB ask we'd pay
    drift:           float   # (curr - ref) / ref  (unsigned; direction-adjusted internally)
    drift_signed:    float   # positive = favourable for direction
    ref_price:       float   # asset price at market resolution-window open
    curr_price:      float   # current Coinbase price
    secs_remaining:  float   # seconds until market closes
    vol_daily:       float   # realised daily vol used
    sigma_tau:       float   # σ(τ) = vol × √(τ/86400)
    has_edge:        bool    # ev >= MIN_EV_THRESHOLD

    def summary(self) -> str:
        return (f"ev={self.ev:+.3f} p={self.p_win:.3f} ask={self.ask:.3f} "
                f"drift={self.drift_signed*100:+.3f}% σ={self.sigma_tau*100:.3f}% "
                f"τ={self.secs_remaining:.0f}s")


# ── caches ───────────────────────────────────────────────────────────────────
_ref_cache:  Dict[str, Tuple[float, float]] = {}   # condition_id → (ref_price, fetched_at)
_vol_cache:  Dict[str, Tuple[float, float]] = {}   # asset        → (vol_daily, fetched_at)
_price_cache: Dict[str, Tuple[float, float]] = {}  # asset        → (price, fetched_at)

_REF_TTL   = 3600   # reference price doesn't change during a market's life
_VOL_TTL   = 900    # recompute vol every 15 min
_PRICE_TTL = 10     # current price stales fast

# ── volatility ───────────────────────────────────────────────────────────────
async def _get_vol(asset: str, client: httpx.AsyncClient) -> float:
    """Realised daily vol from last 60 min of Coinbase 1-min candles."""
    now = time.time()
    cached = _vol_cache.get(asset)
    if cached and now - cached[1] < _VOL_TTL:
        return cached[0]
    try:
        symbol = asset + "-USD"
        end    = int(now)
        start  = end - 3600
        r = await client.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"start": start, "end": end, "granularity": 60},
            timeout=8,
        )
        candles = r.json()
        if not isinstance(candles, list) or len(candles) < 5:
            raise ValueError("too few candles")
        closes  = [float(c[4]) for c in candles]
        returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
        vol_per_min = (sum(x**2 for x in returns) / len(returns)) ** 0.5
        vol_daily   = vol_per_min * math.sqrt(1440)
        _vol_cache[asset] = (vol_daily, now)
        logger.debug(f"vol {asset}: {vol_daily*100:.3f}%/day ({len(returns)} returns)")
        return vol_daily
    except Exception as e:
        logger.debug(f"vol fallback {asset}: {e}")
        return ASSET_VOL_DAILY.get(asset, 0.025)


# ── reference price ───────────────────────────────────────────────────────────
async def _get_ref_price(
    asset: str,
    condition_id: str,
    market_start_ts: float,
    client: httpx.AsyncClient,
) -> Optional[float]:
    """
    Price at the exact start of the resolution window.
    Uses Coinbase 1-min candle OPEN aligned to the minute boundary.
    Falls back to Binance kline via Frankfurt proxy if Coinbase fails.
    Cached permanently per condition_id (ref price never changes during market life).
    """
    cached = _ref_cache.get(condition_id)
    if cached:
        return cached[0]

    # Align to exact minute boundary (Polymarket markets always start on minute marks)
    start = int(market_start_ts / 60) * 60

    # Try Coinbase first
    symbol = asset + "-USD"
    ref    = None
    try:
        end = start + 180   # fetch 3 candles around start
        r   = await client.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"start": start, "end": end, "granularity": 60},
            timeout=8,
        )
        candles = r.json()
        if isinstance(candles, list) and candles:
            best = min(candles, key=lambda c: abs(int(c[0]) - start))
            ref  = float(best[3])   # open price of matching candle
            logger.debug(f"ref_price {asset}: {ref:.5f} via Coinbase (candle@{best[0]})")
    except Exception as e:
        logger.debug(f"ref_price Coinbase failed {asset}: {e}")

    # Fallback: Binance 1-min kline via Frankfurt proxy
    if ref is None:
        bn_sym = _BN_SYMBOL.get(asset)
        if bn_sym and _BINANCE_PROXY:
            try:
                start_ms = start * 1000
                r = await client.get(
                    f"{_BINANCE_PROXY}/api/v3/klines",
                    params={
                        "symbol":    bn_sym,
                        "interval":  "1m",
                        "startTime": start_ms - 60_000,
                        "endTime":   start_ms + 120_000,
                        "limit":     3,
                        "token":     _PROXY_TOKEN,
                    },
                    timeout=8,
                )
                data = r.json()
                if isinstance(data, list) and data:
                    best = min(data, key=lambda c: abs(int(c[0]) - start_ms))
                    ref  = float(best[1])   # open price
                    logger.debug(f"ref_price {asset}: {ref:.5f} via Binance fallback")
            except Exception as e:
                logger.debug(f"ref_price Binance failed {asset}: {e}")

    if ref is not None:
        _ref_cache[condition_id] = (ref, time.time())
    return ref


# ── WebSocket price helper ────────────────────────────────────────────────────
def _get_ws_price(asset: str) -> Optional[float]:
    """Read from CoinbaseWS singleton if available — instant, no I/O."""
    try:
        from coinbase_ws import get_instance
        ws = get_instance()
        if ws:
            return ws.get_price(asset)
    except ImportError:
        pass
    return None


# ── current price ─────────────────────────────────────────────────────────────
async def _get_curr_price(asset: str, client: httpx.AsyncClient) -> Optional[float]:
    """
    Latest asset price. Priority:
      1. CoinbaseWS (instantaneous, no I/O — eliminates REST latency)
      2. Coinbase REST ticker
      3. Binance kline via Frankfurt proxy (geo-blocked Coinbase fallback)
    """
    now = time.time()
    cached = _price_cache.get(asset)
    if cached and now - cached[1] < _PRICE_TTL:
        return cached[0]

    # 1. WebSocket (fastest — updated every tick with no I/O)
    ws_price = _get_ws_price(asset)
    if ws_price:
        _price_cache[asset] = (ws_price, now)
        return ws_price

    # 2. Coinbase REST ticker
    symbol = asset + "-USD"
    try:
        r = await client.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/ticker",
            timeout=6,
        )
        data  = r.json()
        price = float(data["price"])
        _price_cache[asset] = (price, now)
        return price
    except Exception:
        pass

    # 3. Binance kline via Frankfurt proxy
    bn_sym = _BN_SYMBOL.get(asset)
    if bn_sym and _BINANCE_PROXY:
        try:
            r = await client.get(
                f"{_BINANCE_PROXY}/api/v3/ticker/price",
                params={"symbol": bn_sym, "token": _PROXY_TOKEN},
                timeout=6,
            )
            data  = r.json()
            price = float(data["price"])
            _price_cache[asset] = (price, now)
            logger.debug(f"curr_price {asset}: Binance fallback {price:.5f}")
            return price
        except Exception:
            pass

    logger.debug(f"curr_price {asset}: all sources failed")
    return None


# ── CLOB ask ──────────────────────────────────────────────────────────────────
async def _get_clob_ask(token_id: str, client: httpx.AsyncClient) -> Optional[float]:
    """Best ask from Polymarket CLOB for the token we would buy."""
    try:
        r = await client.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=8,
        )
        book = r.json()
        asks = book.get("asks") or book.get("ask") or []
        if not asks:
            return None
        if isinstance(asks[0], dict):
            return float(min(asks, key=lambda x: float(x.get("price", 9)))["price"])
        return float(min(float(a[0]) for a in asks))
    except Exception as e:
        logger.debug(f"CLOB ask failed {token_id[:20]}: {e}")
        return None


# ── Brownian bridge P(win) ────────────────────────────────────────────────────
def _pwin(drift_signed: float, secs_remaining: float, vol_daily: float) -> Tuple[float, float]:
    """
    P(outcome stays in winning direction at resolution | current drift, τ remaining).

    Returns (p_win, sigma_tau).

    With 0 time left and positive drift → P=1.0 (already resolved).
    With 0 drift → P=0.50 (coin flip).
    Higher drift / lower σ(τ) → higher certainty.
    """
    if secs_remaining <= 0:
        return (1.0 if drift_signed > 0 else 0.0), 0.0

    sigma_tau = vol_daily * math.sqrt(secs_remaining / 86400.0)
    if sigma_tau < 1e-9:
        return (1.0 if drift_signed > 0 else 0.0), sigma_tau

    z = drift_signed / sigma_tau
    p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    p = max(0.01, min(0.99, p))
    return p, sigma_tau


# ── public API ────────────────────────────────────────────────────────────────
async def compute_ev(
    asset:                str,
    direction:            str,          # "Up" or "Down"
    token_id:             str,
    condition_id:         str,
    end_time:             float,        # market close epoch seconds
    market_duration_secs: int,          # 300=5-min, 900=15-min
    client:               httpx.AsyncClient,
) -> EVResult:
    """
    Compute expected value for entering this position right now.
    All four fetches run in parallel; failures return neutral EV (no edge).
    """
    # Align to exact minute boundary (matches Polymarket resolution window start)
    market_start_ts = int((end_time - market_duration_secs) / 60) * 60
    now             = time.time()
    secs_remaining  = max(0.0, end_time - now)

    # If we're before the resolution window has started, ref price isn't set yet.
    # Return ev=0, has_edge=False; caller can wait for window.
    if now < market_start_ts:
        secs_to_window = market_start_ts - now
        logger.debug(f"{asset} {direction}: {secs_to_window:.0f}s before resolution window — waiting")
        return EVResult(
            ev=0.0, p_win=0.5, ask=0.5,
            drift=0.0, drift_signed=0.0,
            ref_price=0.0, curr_price=0.0,
            secs_remaining=secs_remaining,
            vol_daily=ASSET_VOL_DAILY.get(asset, 0.025),
            sigma_tau=0.0, has_edge=False,
        )

    # Parallel fetches
    ref_p, curr_p, ask_p, vol_p = await asyncio.gather(
        _get_ref_price(asset, condition_id, market_start_ts, client),
        _get_curr_price(asset, client),
        _get_clob_ask(token_id, client),
        _get_vol(asset, client),
        return_exceptions=True,
    )

    # Sanitise failures
    def _ok(v, fallback):
        return fallback if (v is None or isinstance(v, Exception)) else v

    ref_price  = _ok(ref_p,  None)
    curr_price = _ok(curr_p, None)
    ask        = _ok(ask_p,  None)
    vol_daily  = _ok(vol_p,  ASSET_VOL_DAILY.get(asset, 0.025))

    if ref_price is None or curr_price is None or ask is None:
        logger.debug(f"{asset} {direction}: EV data incomplete ref={ref_price} curr={curr_price} ask={ask}")
        return EVResult(
            ev=0.0, p_win=0.5, ask=ask or 0.5,
            drift=0.0, drift_signed=0.0,
            ref_price=ref_price or 0.0, curr_price=curr_price or 0.0,
            secs_remaining=secs_remaining, vol_daily=vol_daily,
            sigma_tau=0.0, has_edge=False,
        )

    # Compute direction-adjusted drift
    raw_drift    = (curr_price - ref_price) / ref_price   # positive = price went up
    drift_signed = raw_drift if direction == "Up" else -raw_drift   # positive = good for us

    # P(win) and EV
    p_win, sigma_tau = _pwin(drift_signed, secs_remaining, vol_daily)
    ev               = p_win * PAYOUT_RATE - ask
    has_edge         = ev >= MIN_EV_THRESHOLD

    result = EVResult(
        ev=ev, p_win=p_win, ask=ask,
        drift=raw_drift, drift_signed=drift_signed,
        ref_price=ref_price, curr_price=curr_price,
        secs_remaining=secs_remaining, vol_daily=vol_daily,
        sigma_tau=sigma_tau, has_edge=has_edge,
    )
    logger.debug(f"EV {asset} {direction}: {result.summary()}")
    return result


async def wait_for_ev_window(
    asset:                str,
    direction:            str,
    token_id:             str,
    condition_id:         str,
    end_time:             float,
    market_duration_secs: int,
    client:               httpx.AsyncClient,
    timeout_secs:         int = EV_WINDOW_TIMEOUT,
) -> Optional[EVResult]:
    """
    Poll compute_ev every EV_POLL_INTERVAL seconds until EV > threshold or timeout.
    Used by W-bot after a whale signal fires but the EV window hasn't opened yet
    (e.g. whale traded before the resolution window started).
    Returns None if no window found.
    """
    deadline = time.time() + timeout_secs
    min_secs  = 20   # stop polling if market closes in < 20s (too late to enter)

    while True:
        now = time.time()
        if now >= deadline or (end_time - now) < min_secs:
            return None

        result = await compute_ev(
            asset, direction, token_id, condition_id,
            end_time, market_duration_secs, client,
        )
        if result.has_edge:
            logger.info(
                f"EV window: {asset} {direction} | {result.summary()}"
            )
            return result

        await asyncio.sleep(EV_POLL_INTERVAL)


def detect_market_duration(title: str, secs_remaining: float) -> int:
    """
    Infer market resolution window duration (300 or 900 seconds) from title.
    Falls back to secs_remaining heuristic if title parse fails.

    Title example: 'Dogecoin Up or Down - April 9, 7:20PM-7:25PM ET'
    """
    times = re.findall(r'(\d{1,2}):(\d{2})(AM|PM)', title.upper())
    if len(times) >= 2:
        def to_mins(h, m, ap):
            h = int(h); m = int(m)
            if ap == 'PM' and h != 12: h += 12
            if ap == 'AM' and h == 12: h = 0
            return h * 60 + m
        diff = (to_mins(*times[1]) - to_mins(*times[0])) % (24 * 60)
        if diff > 0:
            return diff * 60   # e.g. 5 min → 300, 15 min → 900

    # Heuristic: if a lot of time is left it's likely a 15-min market
    return 900 if secs_remaining > 600 else 300
