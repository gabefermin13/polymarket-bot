"""
direction_signals.py — Live signal functions for D-bot consensus system

S1: Price action        (Coinbase 5-min candle momentum)
S2: Order flow          (Coinbase buyer/seller imbalance)
S3: Market structure    (OKX funding rate; Coinglass aggregate if key set)
S4: Liquidation cascade (OKX liquidation-orders, $75K threshold)
S5: Cross-asset align   (BTC+ETH+SOL agreeing; ≥2/3 required)
S6: Polymarket flow     (data-api.polymarket.com net CLOB volume)
S7: Whale consensus     (reads C-bot signals.jsonl, last N seconds)

All async functions (except S7 which is sync fast local I/O):
    signature:  async def sN_xxx(client, asset, ts_ms=None) -> tuple[str, float]
    direction:  "UP" | "DOWN" | "FLAT" | "NEUTRAL" | "NONE" | "BALANCED"
    conf:       0.55–0.95

S7 signature: s7_whale_consensus(asset, direction, ts_ms=None) -> tuple[str, float]
    direction param: "Up" or "Down"  (Polymarket convention)
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

logger = logging.getLogger("direction_signals")

# ── Thresholds (matching scan_directional_fast.py exactly) ──────────────────
MOMENTUM_THRESHOLD = 0.0008
ORDER_FLOW_IMBAL   = 0.20
FUNDING_THRESHOLD  = 0.0001
LIQ_USD_THRESHOLD  = 75_000
POLY_FLOW_RATIO    = 0.60

# ── S11: Binance CVD via Frankfurt proxy ─────────────────────────────────────
_BINANCE_PROXY = os.getenv("BINANCE_PROXY", "http://138.197.181.139:8081")
_PROXY_TOKEN   = os.getenv("PROXY_TOKEN",   "poly_binance_proxy_2026")
_CVD_THRESHOLD = float(os.getenv("CVD_THRESHOLD", "0.08"))  # net buy fraction for signal
_BN_SYMBOL     = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "DOGE": "DOGEUSDT", "XRP": "XRPUSDT"}

# ── TTL cache ────────────────────────────────────────────────────────────────
_cache: dict = {}

_TTL = {
    "s1":  300,   # 5-min candle bucket — no point refreshing faster
    "s2":  30,    # live order flow
    "s3":  1800,  # funding rate changes every 8h; 30 min cache is fine
    "s4":  60,    # liquidation window
    "s5":  300,   # cross-asset depends on S1 (5-min buckets)
    "s6":  30,    # live poly flow
    "s11": 60,    # CVD: 1-min window, refresh every minute
}

def _cache_get(sig: str, key: tuple):
    entry = _cache.get((sig, key))
    if entry and time.time() - entry["ts"] < _TTL.get(sig, 60):
        return entry["val"]
    return None

def _cache_set(sig: str, key: tuple, val):
    _cache[(sig, key)] = {"val": val, "ts": time.time()}


def _5min_bucket(ts_ms: int) -> int:
    return (ts_ms // 300_000) * 300_000


# ── HTTP helper ──────────────────────────────────────────────────────────────
async def _get(client, url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = await client.get(url, params=params, timeout=15.0)
            if r.status_code == 429:
                await asyncio.sleep(2 + attempt * 2)
                continue
            if r.status_code != 200:
                return None
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                logger.debug(f"GET {url} failed: {e}")
            await asyncio.sleep(1)
    return None


# ── S1: Price action ─────────────────────────────────────────────────────────

async def s1_price_action(client, asset: str, ts_ms: int = None) -> tuple[str, float]:
    """5-min candle momentum via Coinbase REST."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    symbol = f"{asset}-USD"
    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s1", (symbol, bucket))
    if cached is not None:
        return cached

    end_ts    = bucket // 1000
    start_ts  = end_ts - 300
    end_iso   = datetime.fromtimestamp(end_ts, tz=timezone.utc).isoformat()
    start_iso = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
    data = await _get(client,
        f"https://api.exchange.coinbase.com/products/{symbol}/candles",
        params={"granularity": 300, "start": start_iso, "end": end_iso})

    result = ("FLAT", 0.57)
    if data and isinstance(data, list):
        try:
            c = data[0]
            open_p, close_p = float(c[3]), float(c[4])
            if open_p != 0:
                momentum = (close_p - open_p) / open_p
                if momentum > MOMENTUM_THRESHOLD:
                    conf = min(0.55 + abs(momentum) / (MOMENTUM_THRESHOLD * 5) * 0.20, 0.75)
                    result = ("UP", round(conf, 3))
                elif momentum < -MOMENTUM_THRESHOLD:
                    conf = min(0.55 + abs(momentum) / (MOMENTUM_THRESHOLD * 5) * 0.20, 0.75)
                    result = ("DOWN", round(conf, 3))
        except Exception:
            pass
    _cache_set("s1", (symbol, bucket), result)
    return result


# ── S2: Order flow ────────────────────────────────────────────────────────────

async def s2_order_flow(client, asset: str, ts_ms: int = None) -> tuple[str, float]:
    """Coinbase /trades buyer-vs-seller imbalance in ±60s window."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    symbol = f"{asset}-USD"
    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s2", (symbol, bucket))
    if cached is not None:
        return cached

    data = await _get(client,
        f"https://api.exchange.coinbase.com/products/{symbol}/trades",
        params={"limit": 100})

    result = ("NEUTRAL", 0.57)
    if data and isinstance(data, list):
        window_start = ts_ms - 60_000
        window_end   = ts_ms + 60_000
        buy_vol = sell_vol = 0.0
        for t in data:
            try:
                t_ts = int(datetime.fromisoformat(
                    t["time"].replace("Z", "+00:00")).timestamp() * 1000)
            except Exception:
                continue
            if not (window_start <= t_ts <= window_end):
                continue
            size = float(t.get("size", 0))
            if t.get("side") == "buy":
                buy_vol += size
            else:
                sell_vol += size
        total = buy_vol + sell_vol
        if total > 0:
            imbal = (buy_vol - sell_vol) / total
            if imbal > ORDER_FLOW_IMBAL:
                result = ("UP", round(min(0.55 + abs(imbal) * 0.30, 0.78), 3))
            elif imbal < -ORDER_FLOW_IMBAL:
                result = ("DOWN", round(min(0.55 + abs(imbal) * 0.30, 0.78), 3))
    _cache_set("s2", (symbol, bucket), result)
    return result


# ── S3: Market structure (funding rate) ───────────────────────────────────────

_COINGLASS_KEY = os.getenv("COINGLASS_API_KEY", "")

async def s3_market_structure(client, asset: str, ts_ms: int = None) -> tuple[str, float]:
    """OKX funding rate (negative = longs paying shorts = bullish bias → UP)."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s3", (asset, bucket))
    if cached is not None:
        return cached

    rate_val = None
    if _COINGLASS_KEY:
        data = await _get(client,
            "https://open-api.coinglass.com/public/v2/funding",
            params={"symbol": asset})
        if data and isinstance(data, dict) and data.get("success"):
            rates = [float(e.get("rate") or 0)
                     for e in data.get("data", []) if e.get("rate") is not None]
            if rates:
                rate_val = sum(rates) / len(rates)

    if rate_val is None:
        inst_id = f"{asset}-USDT-SWAP"
        data = await _get(client,
            "https://www.okx.com/api/v5/public/funding-rate-history",
            params={"instId": inst_id, "limit": 1})
        if data and isinstance(data, dict):
            items = data.get("data", [])
            if items:
                rate_val = float(items[0].get("fundingRate", 0))

    result = ("NEUTRAL", 0.57)
    if rate_val is not None:
        try:
            if rate_val < -FUNDING_THRESHOLD:
                conf = min(0.57 + abs(rate_val) / 0.001 * 0.15, 0.72)
                result = ("UP", round(conf, 3))
            elif rate_val > FUNDING_THRESHOLD:
                conf = min(0.57 + abs(rate_val) / 0.001 * 0.15, 0.72)
                result = ("DOWN", round(conf, 3))
        except Exception:
            pass
    _cache_set("s3", (asset, bucket), result)
    return result


# ── S4: Liquidation cascades ──────────────────────────────────────────────────

async def s4_liquidations(client, asset: str, ts_ms: int = None) -> tuple[str, float]:
    """OKX liquidation-orders: long liquidations → DOWN, short liquidations → UP."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    uly    = f"{asset}-USDT"
    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s4", (uly, bucket))
    if cached is not None:
        return cached

    # OKX only returns recent data (≤7 days)
    cutoff_ms = int(time.time() * 1000) - 7 * 86_400 * 1000
    if ts_ms < cutoff_ms:
        return ("NONE", 0.60)

    data = await _get(client,
        "https://www.okx.com/api/v5/public/liquidation-orders",
        params={"instType": "SWAP", "uly": uly, "state": "filled", "limit": 50})

    details = []
    if data and isinstance(data, dict):
        for item in data.get("data", []):
            details.extend(item.get("details", []))

    result = ("NONE", 0.60)
    if details:
        window_start = ts_ms - 180_000
        long_liq = short_liq = 0.0
        for order in details:
            try:
                o_ts = int(order.get("time") or order.get("ts") or 0)
                if not (window_start <= o_ts <= ts_ms):
                    continue
                usd = float(order.get("sz", 0)) * float(order.get("bkPx", 0))
                if order.get("posSide") == "long" and order.get("side") == "sell":
                    long_liq += usd
                elif order.get("posSide") == "short" and order.get("side") == "buy":
                    short_liq += usd
            except Exception:
                continue
        if long_liq > LIQ_USD_THRESHOLD:
            result = ("DOWN", round(min(0.60 + long_liq / LIQ_USD_THRESHOLD * 0.08, 0.82), 3))
        elif short_liq > LIQ_USD_THRESHOLD:
            result = ("UP", round(min(0.60 + short_liq / LIQ_USD_THRESHOLD * 0.08, 0.82), 3))
    _cache_set("s4", (uly, bucket), result)
    return result


# ── S5: Cross-asset alignment ─────────────────────────────────────────────────

async def s5_cross_asset(client, primary_asset: str, ts_ms: int = None) -> tuple[str, float]:
    """
    Fetches 5-min momentum for BTC, ETH, SOL simultaneously.
    Signals UP/DOWN only if ≥2/3 assets agree AND the primary asset is in the majority.
    conf: 0.60 (2/3) or 0.73 (3/3).
    Returns FLAT if primary asset disagrees with the majority or no majority exists.
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s5", (primary_asset, bucket))
    if cached is not None:
        return cached

    btc_r, eth_r, sol_r = await asyncio.gather(
        s1_price_action(client, "BTC", ts_ms),
        s1_price_action(client, "ETH", ts_ms),
        s1_price_action(client, "SOL", ts_ms),
    )
    directions = {"BTC": btc_r[0], "ETH": eth_r[0], "SOL": sol_r[0]}

    primary_dir = directions.get(primary_asset)
    if primary_dir in ("FLAT", None):
        result = ("FLAT", 0.57)
        _cache_set("s5", (primary_asset, bucket), result)
        return result

    up_count   = sum(1 for d in directions.values() if d == "UP")
    down_count = sum(1 for d in directions.values() if d == "DOWN")

    result = ("FLAT", 0.57)
    if up_count >= 2 and primary_dir == "UP":
        conf = 0.73 if up_count == 3 else 0.60
        result = ("UP", conf)
    elif down_count >= 2 and primary_dir == "DOWN":
        conf = 0.73 if down_count == 3 else 0.60
        result = ("DOWN", conf)

    _cache_set("s5", (primary_asset, bucket), result)
    return result


# ── S6: Polymarket flow ───────────────────────────────────────────────────────

async def s6_poly_flow(client, token_id: str, ts_ms: int = None) -> tuple[str, float]:
    """
    Net buy/sell flow for the specific token via data-api.polymarket.com.
    UP = net buying of this token (market agrees with whoever holds it).
    Pass empty token_id ("") to skip and return BALANCED.
    """
    if not token_id:
        return ("BALANCED", 0.57)
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s6", (token_id, bucket))
    if cached is not None:
        return cached

    data = await _get(client,
        "https://data-api.polymarket.com/trades",
        params={"tokenId": token_id, "limit": 100})

    result = ("BALANCED", 0.57)
    if data and isinstance(data, list):
        window_start = ts_ms - 60_000
        buy_vol = sell_vol = 0.0
        for t in data:
            try:
                t_ts = int(t.get("timestamp") or 0)
                if not (window_start <= t_ts <= ts_ms):
                    continue
                size = float(t.get("size") or 0)
                if t.get("side") == "BUY":
                    buy_vol += size
                elif t.get("side") == "SELL":
                    sell_vol += size
            except Exception:
                continue
        total = buy_vol + sell_vol
        if total > 0:
            ratio = buy_vol / total
            if ratio > POLY_FLOW_RATIO:
                result = ("UP", round(min(0.56 + (ratio - POLY_FLOW_RATIO) * 0.70, 0.70), 3))
            elif ratio < (1 - POLY_FLOW_RATIO):
                result = ("DOWN", round(min(0.56 + ((1 - POLY_FLOW_RATIO) - ratio) * 0.70, 0.70), 3))
    _cache_set("s6", (token_id, bucket), result)
    return result


# ── S7: Whale consensus tap ───────────────────────────────────────────────────

_CBOT_SIGNAL_PATHS = [p for p in [
    os.getenv("CBOT_SIGNALS_C1", "/root/kalshiedge_whalewallet_c1/logs_c1/signals.jsonl"),
    os.getenv("CBOT_SIGNALS_C2", "/root/kalshiedge_whalewallet/logs/signals.jsonl"),
    os.getenv("CBOT_SIGNALS_C3", "/root/kalshiedge_whalewallet_c3/logs_c3/signals.jsonl"),
    os.getenv("CBOT_SIGNALS_C4", "/root/kalshiedge_whalewallet_c4/logs_c4/signals.jsonl"),
] if p]

S7_LOOKBACK_SECS = float(os.getenv("S7_LOOKBACK_SECS", "120"))


def s7_whale_consensus(asset: str, direction: str, ts_ms: int = None) -> tuple[str, float]:
    """
    Sync. Reads the last ~50 KB of each C-bot signals.jsonl and looks for
    an executed consensus signal for (asset, direction) within S7_LOOKBACK_SECS.

    asset:      "BTC", "ETH", "SOL"
    direction:  "Up" or "Down"   (Polymarket convention)
    Returns ("UP"/"DOWN", conf) or ("NONE", 0.57).
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    cutoff_ms  = ts_ms - int(S7_LOOKBACK_SECS * 1000)
    best_conf  = None
    asset_up   = asset.upper()
    dir_lower  = direction.lower()   # "up" or "down"

    for path in _CBOT_SIGNAL_PATHS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 50_000))
                lines = fh.read().decode("utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            # Only executed (consensus-reached) signals
            if not rec.get("executed"):
                continue

            rec_ts = int(rec.get("ts") or 0)
            if rec_ts < cutoff_ms:
                break  # lines are time-ordered oldest→newest; stop scanning

            # Asset match: check symbol ("BTC-USD"), asset field, or title
            symbol_field = (rec.get("symbol") or rec.get("asset") or "").upper()
            title_field  = (rec.get("title") or "").lower()
            if asset_up not in symbol_field:
                asset_name = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}.get(asset_up, "")
                if asset_up.lower() not in title_field and asset_name not in title_field:
                    continue

            # Direction match
            rec_dir = (rec.get("direction") or "").lower()
            if rec_dir != dir_lower:
                continue

            # Extract best confidence
            conf_val = float(
                rec.get("best_conf") or rec.get("confidence") or rec.get("conf") or 0.75
            )
            if best_conf is None or conf_val > best_conf:
                best_conf = conf_val

    if best_conf is not None:
        return (direction.upper(), round(min(max(best_conf, 0.75), 0.95), 3))
    return ("NONE", 0.57)


# ── S11: CVD (Cumulative Volume Delta via Binance aggTrades) ──────────────────

async def s11_cvd(client, asset: str, ts_ms: int = None) -> tuple[str, float]:
    """
    Net buy/sell pressure over the last 5 minutes from Binance aggTrades,
    fetched via Frankfurt proxy (bypasses NYC VPS geo-block).

    m=True  → buyer is maker → seller-initiated → SELL pressure
    m=False → seller is maker → buyer-initiated → BUY pressure

    Returns UP/DOWN/NEUTRAL based on net buy fraction vs _CVD_THRESHOLD.
    conf: 0.55–0.75 (scaled by imbalance magnitude).
    """
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    symbol = _BN_SYMBOL.get(asset)
    if not symbol or not _BINANCE_PROXY:
        return ("NEUTRAL", 0.57)

    bucket = _5min_bucket(ts_ms)
    cached = _cache_get("s11", (symbol, bucket))
    if cached is not None:
        return cached

    data = await _get(
        client,
        f"{_BINANCE_PROXY}/api/v3/aggTrades",
        params={"symbol": symbol, "limit": 1000, "token": _PROXY_TOKEN},
    )

    result = ("NEUTRAL", 0.57)
    if data and isinstance(data, list):
        window_start = ts_ms - 300_000   # 5-min window
        buy_vol = sell_vol = 0.0
        for trade in data:
            try:
                t_ts = int(trade.get("T", 0))
                if not (window_start <= t_ts <= ts_ms):
                    continue
                qty            = float(trade.get("q", 0))
                is_buyer_maker = trade.get("m", False)
                if is_buyer_maker:
                    sell_vol += qty   # seller-initiated
                else:
                    buy_vol  += qty   # buyer-initiated
            except Exception:
                continue
        total = buy_vol + sell_vol
        if total > 0:
            imbal = (buy_vol - sell_vol) / total
            if imbal > _CVD_THRESHOLD:
                conf   = min(0.55 + abs(imbal) * 0.25, 0.75)
                result = ("UP", round(conf, 3))
            elif imbal < -_CVD_THRESHOLD:
                conf   = min(0.55 + abs(imbal) * 0.25, 0.75)
                result = ("DOWN", round(conf, 3))

    _cache_set("s11", (symbol, bucket), result)
    return result
