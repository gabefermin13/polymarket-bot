"""
kronos_signal.py — Kronos price forecast as Kelly multiplier.

Calls the Frankfurt proxy /kronos/{symbol} endpoint.
Returns a float multiplier [0.5 - 1.3] to scale Kelly bet size based on:
  - Forecast direction vs signal direction (agrees → boost, opposes → cut)
  - Realized daily vol (low vol → boost, high vol → cut)

Falls back to 1.0 on any error (fail-open, never blocks a trade).

Usage:
    from kronos_signal import get_kronos_kelly
    kronos_mult = await get_kronos_kelly(asset, direction)
    kelly_multiplier *= kronos_mult
"""
import asyncio
import logging
import os
import time

import httpx

FRANKFURT_URL = os.getenv("BINANCE_PROXY", "http://138.197.181.139:8081")
PROXY_TOKEN   = os.getenv("PROXY_TOKEN", "poly_binance_proxy_2026")
KRONOS_TTL    = int(os.getenv("KRONOS_TTL", "60"))   # cache seconds per symbol
KRONOS_TIMEOUT = float(os.getenv("KRONOS_TIMEOUT", "12"))  # HTTP timeout

# Thresholds
_HIGH_VOL = float(os.getenv("KRONOS_HIGH_VOL", "0.04"))   # >=4% annualised daily vol
_LOW_VOL  = float(os.getenv("KRONOS_LOW_VOL",  "0.015"))  # <=1.5%

# Direction multipliers
_DIR_AGREE    = float(os.getenv("KRONOS_DIR_AGREE",    "1.15"))
_DIR_DISAGREE = float(os.getenv("KRONOS_DIR_DISAGREE", "0.65"))

# Vol multipliers
_VOL_HIGH_MULT = float(os.getenv("KRONOS_VOL_HIGH", "0.85"))
_VOL_LOW_MULT  = float(os.getenv("KRONOS_VOL_LOW",  "1.15"))

_cache: dict = {}  # symbol -> (ts, {"direction": ..., "vol_daily": ...})
logger = logging.getLogger(__name__)


async def get_kronos_kelly(symbol: str, signal_direction: str) -> float:
    """
    Returns a Kelly multiplier [0.5 - 1.3].

    symbol           : "BTC", "ETH", "DOGE", "XRP"
    signal_direction : "Up" or "Down"
    """
    now = time.time()

    # Per-symbol cache (direction-agnostic -- same forecast, different mult)
    if symbol in _cache and now - _cache[symbol][0] < KRONOS_TTL:
        data = _cache[symbol][1]
    else:
        data = await _fetch_kronos(symbol)
        if data:
            _cache[symbol] = (now, data)
        else:
            return 1.0   # fail-open

    forecast_dir = data.get("direction")    # "Up" | "Down" | None
    vol_daily    = float(data.get("vol_daily", 0.02))

    # --- Volatility tier ---
    if vol_daily >= _HIGH_VOL:
        vol_mult = _VOL_HIGH_MULT
    elif vol_daily <= _LOW_VOL:
        vol_mult = _VOL_LOW_MULT
    else:
        vol_mult = 1.0

    # --- Direction alignment ---
    if forecast_dir == signal_direction:
        dir_mult = _DIR_AGREE
    elif forecast_dir and forecast_dir != signal_direction:
        dir_mult = _DIR_DISAGREE
    else:
        dir_mult = 1.0

    mult = round(vol_mult * dir_mult, 3)
    mult = max(0.5, min(1.3, mult))

    logger.info(
        f"Kronos {symbol}: forecast={forecast_dir} vol={vol_daily:.3f} "
        f"signal={signal_direction} -> kelly_mult={mult:.2f} "
        f"(vol_mult={vol_mult:.2f} dir_mult={dir_mult:.2f})"
    )
    return mult


async def _fetch_kronos(symbol: str):
    """Fetch forecast from Frankfurt proxy. Returns dict or None on error."""
    try:
        url = f"{FRANKFURT_URL}/kronos/{symbol}"
        async with httpx.AsyncClient(timeout=KRONOS_TIMEOUT) as client:
            r = await client.get(url, params={"token": PROXY_TOKEN})
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.warning(f"Kronos fetch failed for {symbol}: {e} -- using 1.0")
        return None
