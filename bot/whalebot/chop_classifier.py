"""
chop_classifier.py — Detects choppy/ranging price action for a given crypto asset.

Uses Coinbase 1-min candles (last N candles, default 20) to compute three metrics:
  1. range_efficiency  — |net price move| / sum(candle ranges). Low = chop.
  2. return_autocorr   — lag-1 autocorrelation of log returns. Negative = mean-reverting = chop.
  3. dir_frac          — fraction of candles moving in dominant direction. Low = chop.

chop_score in [0, 1]: 0 = strongly trending, 1 = pure chop.
is_chop = chop_score >= CHOP_THRESHOLD (default 0.55).

Cache TTL: 60s per symbol. Fails open (is_chop=False) on any error.
"""
import math
import os
import time
import logging

import httpx

logger = logging.getLogger(__name__)

CHOP_THRESHOLD = float(os.getenv("CHOP_THRESHOLD", "0.55"))
CHOP_CANDLES   = int(os.getenv("CHOP_CANDLES",   "20"))    # 1-min candles to analyze
CHOP_TTL       = int(os.getenv("CHOP_TTL",        "60"))    # cache seconds per symbol

_COINBASE_PAIRS = {
    "BTC":  "BTC-USD",
    "ETH":  "ETH-USD",
    "DOGE": "DOGE-USD",
    "XRP":  "XRP-USD",
    "SOL":  "SOL-USD",
}

_cache: dict = {}   # symbol -> (ts, ChopResult)


class ChopResult:
    __slots__ = ("is_chop", "score", "efficiency", "autocorr", "dir_frac")

    def __init__(self, is_chop: bool, score: float,
                 efficiency: float, autocorr: float, dir_frac: float):
        self.is_chop    = is_chop
        self.score      = score
        self.efficiency = efficiency
        self.autocorr   = autocorr
        self.dir_frac   = dir_frac

    def summary(self) -> str:
        return (f"chop={self.score:.2f} "
                f"(eff={self.efficiency:.2f} corr={self.autocorr:+.2f} dir={self.dir_frac:.2f})")


_NEUTRAL = ChopResult(False, 0.5, 0.5, 0.0, 0.5)


async def classify(symbol: str, http_client=None) -> ChopResult:
    """
    Returns ChopResult for the given crypto symbol.
    http_client: optional httpx.AsyncClient to reuse; if None, creates a short-lived one.
    """
    now = time.time()
    if symbol in _cache and now - _cache[symbol][0] < CHOP_TTL:
        return _cache[symbol][1]

    candles = await _fetch_candles(symbol, http_client)
    result  = _compute(candles)
    _cache[symbol] = (now, result)
    return result


async def _fetch_candles(symbol: str, client=None):
    pair   = _COINBASE_PAIRS.get(symbol, f"{symbol}-USD")
    url    = f"https://api.exchange.coinbase.com/products/{pair}/candles"
    params = {"granularity": 60, "limit": CHOP_CANDLES}
    try:
        if client is not None:
            r    = await client.get(url, params=params, timeout=8.0)
            data = r.json()
        else:
            async with httpx.AsyncClient(timeout=8.0) as c:
                r    = await c.get(url, params=params)
                data = r.json()
        # Coinbase format: [[ts, low, high, open, close, vol], ...] newest first
        data.sort(key=lambda x: x[0])   # oldest first
        return data
    except Exception as exc:
        logger.warning(f"ChopClassifier: candle fetch failed for {symbol}: {exc}")
        return []


def _compute(candles) -> ChopResult:
    if len(candles) < 5:
        return _NEUTRAL

    opens  = [c[3] for c in candles]
    highs  = [c[2] for c in candles]
    lows   = [c[1] for c in candles]
    closes = [c[4] for c in candles]
    n      = len(candles)

    # 1. Range efficiency: low = chop
    net_move    = abs(closes[-1] - opens[0])
    total_range = sum(highs[i] - lows[i] for i in range(n))
    efficiency  = (net_move / total_range) if total_range > 1e-10 else 0.5

    # 2. Lag-1 return autocorrelation: negative = mean-reverting = chop
    returns = []
    for i in range(1, n):
        if closes[i - 1] > 0:
            returns.append(math.log(closes[i] / closes[i - 1]))
    if len(returns) >= 4:
        mean_r = sum(returns) / len(returns)
        r0     = [r - mean_r for r in returns[:-1]]
        r1     = [r - mean_r for r in returns[1:]]
        num    = sum(a * b for a, b in zip(r0, r1))
        den    = math.sqrt(sum(a * a for a in r0) * sum(b * b for b in r1))
        autocorr = (num / den) if den > 1e-10 else 0.0
    else:
        autocorr = 0.0

    # 3. Directional fraction: 0.5 = split evenly = chop; 1.0 = all same dir = trend
    up_count = sum(1 for i in range(n) if closes[i] > opens[i])
    dir_frac  = max(up_count, n - up_count) / n

    # Map each metric → chop contribution in [0, 1]
    eff_chop  = max(0.0, min(1.0, 1.0 - efficiency))               # low eff → high chop
    corr_chop = max(0.0, min(1.0, 0.5 - autocorr / 2.0))           # neg corr → high chop
    dir_chop  = max(0.0, min(1.0, 1.0 - (dir_frac - 0.5) * 2.0))  # 50/50 split → high chop

    score   = 0.40 * eff_chop + 0.35 * corr_chop + 0.25 * dir_chop
    is_chop = score >= CHOP_THRESHOLD

    return ChopResult(
        is_chop,
        round(score,      3),
        round(efficiency, 3),
        round(autocorr,   3),
        round(dir_frac,   3),
    )
