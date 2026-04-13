"""
polymarket_data.py — Shared Polymarket API data layer (D2 + W-bot).

Wraps Gamma API, Data API, and CLOB API with TTL caching and safe fallbacks.
All public functions return fallback values on error — never raise.

Usage:
    import polymarket_data
    liquidity = await polymarket_data.get_liquidity(condition_id)
    buy_vol, sell_vol = await polymarket_data.get_net_flow(token_id)
    bid_usd, ask_usd = await polymarket_data.get_orderbook(token_id)
    await polymarket_data.close()   # call on bot shutdown
"""
import logging
import os
import time

import httpx

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

LIQUIDITY_TTL  = int(os.getenv("POLY_LIQUIDITY_TTL",  "120"))
FLOW_TTL       = int(os.getenv("POLY_FLOW_TTL",        "30"))
ORDERBOOK_TTL  = int(os.getenv("POLY_ORDERBOOK_TTL",   "15"))
FLOW_WINDOW    = 300        # seconds of trade history
FLOW_MIN_VOL   = 5.0        # $5 minimum total weighted volume before trusting
FLOW_RECENCY_W = 2.0        # trades in last 60s count this many times more

_GAMMA_URL     = "https://gamma-api.polymarket.com/markets"
_DATA_URL      = "https://data-api.polymarket.com/trades"
_ORDERBOOK_URL = "https://clob.polymarket.com/book"

# ── Cache ─────────────────────────────────────────────────────────────────────

_cache: dict = {}   # key → (value, expires_at)

def _cache_get(key):
    entry = _cache.get(key)
    if entry is not None and time.time() < entry[1]:
        return entry[0]
    return None

def _cache_set(key, value, ttl: int):
    _cache[key] = (value, time.time() + ttl)

# ── HTTP session ──────────────────────────────────────────────────────────────

_session: httpx.AsyncClient | None = None

async def _get_session() -> httpx.AsyncClient:
    global _session
    if _session is None or _session.is_closed:
        _session = httpx.AsyncClient(timeout=10.0)
    return _session

async def close():
    """Close the shared HTTP session. Call on bot shutdown."""
    global _session
    if _session and not _session.is_closed:
        await _session.aclose()
        _session = None

# ── Public API ────────────────────────────────────────────────────────────────

async def get_liquidity(condition_id: str) -> float:
    """
    Current USDC liquidity in the market pool via Gamma API.
    Returns 0.0 on error or empty response.
    """
    key = ("liquidity", condition_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    val = 0.0
    error = False
    try:
        session = await _get_session()
        r = await session.get(_GAMMA_URL, params={"condition_id": condition_id})
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data, list) else data.get("data", [])
        if markets:
            val = float(markets[0].get("liquidityNum") or 0)
    except Exception as e:
        logger.warning(f"[poly_data] get_liquidity({condition_id[:12]}): {e}")
        error = True

    _cache_set(key, val, 10 if error else LIQUIDITY_TTL)
    return val


async def get_net_flow(token_id: str) -> tuple[float, float]:
    """
    Recency-weighted (buy_vol, sell_vol) over last FLOW_WINDOW seconds.
    Trades within last 60s count FLOW_RECENCY_W× more.
    Returns (0.0, 0.0) when total weighted volume < FLOW_MIN_VOL or on error.
    """
    key = ("flow", token_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    fallback = (0.0, 0.0)
    try:
        session = await _get_session()
        r = await session.get(_DATA_URL, params={"tokenId": token_id, "limit": 200})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logger.warning(f"[poly_data] get_net_flow({token_id[:16]}): {e}")
        _cache_set(key, fallback, FLOW_TTL)
        return fallback

    now_ms            = int(time.time() * 1000)
    window_start_ms   = now_ms - FLOW_WINDOW * 1000
    recency_cutoff_ms = now_ms - 60_000

    buy_vol = sell_vol = 0.0
    if isinstance(data, list):
        for t in data:
            try:
                raw_ts = int(t.get("timestamp") or 0)
                t_ms   = raw_ts if raw_ts > 1_000_000_000_000 else raw_ts * 1000
                if not (window_start_ms <= t_ms <= now_ms):
                    continue
                size   = float(t.get("size") or 0)
                weight = FLOW_RECENCY_W if t_ms >= recency_cutoff_ms else 1.0
                side   = t.get("side", "")
                if side == "BUY":
                    buy_vol  += size * weight
                elif side == "SELL":
                    sell_vol += size * weight
            except Exception:
                continue

    total  = buy_vol + sell_vol
    result = (round(buy_vol, 4), round(sell_vol, 4)) if total >= FLOW_MIN_VOL else fallback
    _cache_set(key, result, FLOW_TTL)
    return result


async def get_orderbook(token_id: str) -> tuple[float, float]:
    """
    (bid_dollars, ask_dollars) — total dollar depth on each side.
    Returns (0.0, 0.0) on error.
    """
    key = ("orderbook", token_id)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    fallback = (0.0, 0.0)
    try:
        session = await _get_session()
        r = await session.get(_ORDERBOOK_URL, params={"token_id": token_id})
        r.raise_for_status()
        data = r.json()
        bids    = data.get("bids") or []
        asks    = data.get("asks") or []
        bid_usd = sum(float(b.get("price", 0)) * float(b.get("size", 0)) for b in bids)
        ask_usd = sum(float(a.get("price", 0)) * float(a.get("size", 0)) for a in asks)
        result  = (round(bid_usd, 2), round(ask_usd, 2))
    except Exception as e:
        logger.warning(f"[poly_data] get_orderbook({token_id[:16]}): {e}")
        result = fallback

    _cache_set(key, result, ORDERBOOK_TTL)
    return result
