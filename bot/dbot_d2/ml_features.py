"""
ml_features.py — Feature engineering for the XGBoost win-rate model.

Given a market (symbol, direction, start_ts, end_ts, ask_price),
reconstructs all features available at entry time.

All external calls use cutoff_ts = start_ts to prevent leakage.
"""
import asyncio
import math
import time
import os
from dataclasses import dataclass, field
from typing import Optional
import httpx

# ── Constants ────────────────────────────────────────────────────────────────
BINANCE_PROXY   = os.getenv("BINANCE_PROXY", "http://138.197.181.139:8081")
PROXY_TOKEN     = os.getenv("PROXY_TOKEN",   "poly_binance_proxy_2026")
COINBASE_BASE   = "https://api.coinbase.com/v2"
COINBASE_ADV    = "https://api.coinbase.com/api/v3/brokerage"
OKX_BASE        = "https://www.okx.com"

# Symbol maps
_CB_PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "DOGE": "DOGE-USD", "XRP": "XRP-USD"}
_BN_PAIRS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "DOGE": "DOGEUSDT", "XRP": "XRPUSDT"}
_OKX_PAIRS = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP",
              "DOGE": "DOGE-USDT-SWAP", "XRP": "XRP-USDT-SWAP"}

DIRECTION_SIGN = {"Up": 1, "Down": -1}

# ── Feature dataclass ────────────────────────────────────────────────────────
@dataclass
class MarketFeatures:
    # Market metadata
    symbol:          str   = ""
    direction:       str   = ""
    duration_secs:   int   = 300    # 5-min default
    secs_remaining:  float = 300.0
    hour_utc:        int   = 0
    minute_utc:      int   = 0
    day_of_week:     int   = 0      # 0=Mon
    ask_price:       float = 0.5

    # S1: Coinbase 1-min momentum (signed for direction)
    s1_momentum:     float = 0.0    # pct change, signed
    s1_conf:         float = 0.0

    # S2: Order flow imbalance (contrarian — negative = bullish for signal)
    s2_imbalance:    float = 0.0    # buy_vol / total_vol, raw
    s2_conf:         float = 0.0

    # S3: Funding rate
    s3_funding:      float = 0.0    # raw funding rate (positive = longs pay)
    s3_conf:         float = 0.0

    # S4: Liquidations (USD in last 5 min)
    s4_liq_long:     float = 0.0
    s4_liq_short:    float = 0.0
    s4_net_liq:      float = 0.0    # short_liqs - long_liqs (positive = bearish pressure cleared)

    # S5: Cross-asset alignment
    s5_agree_count:  int   = 0      # how many of BTC/ETH/DOGE agree
    s5_conf:         float = 0.0

    # S6: Polymarket CLOB flow (net buy volume in 5-min window)
    s6_net_flow:     float = 0.0    # signed, direction-adjusted
    s6_conf:         float = 0.0

    # S8: Options skew (BTC/ETH only)
    s8_skew:         float = 0.0    # put_iv - call_iv (positive = bearish)
    s8_conf:         float = 0.0

    # S9: Coinbase vs Kraken premium
    s9_premium:      float = 0.0    # (cb - kraken) / kraken

    # S10: Perp/spot basis
    s10_basis:       float = 0.0    # (perp - spot) / spot

    # Drift / EV features
    drift_pct:       float = 0.0    # signed price drift since window open
    drift_signed:    float = 0.0    # drift * direction_sign
    ev:              float = 0.0    # Brownian bridge EV
    p_win:           float = 0.5

    # Binance-sourced features (via Frankfurt proxy)
    bn_momentum_1m:  float = 0.0    # 1-min Binance momentum
    bn_momentum_5m:  float = 0.0    # 5-min Binance momentum
    bn_volume_ratio: float = 1.0    # current 1-min vol / avg 20-min vol
    bn_cvd_1m:       float = 0.0    # cumulative volume delta, 1-min window
    bn_oi_change:    float = 0.0    # open interest % change (5-min)

    # Labels (filled during training, not inference)
    outcome:         Optional[int] = None   # 1=won, 0=lost
    condition_id:    str = ""


# ── HTTP helpers ─────────────────────────────────────────────────────────────
async def _get(client: httpx.AsyncClient, url: str, params: dict = None,
               timeout: float = 8.0) -> dict | list | None:
    try:
        r = await client.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


async def _binance(client: httpx.AsyncClient, path: str, params: dict) -> dict | list | None:
    params["token"] = PROXY_TOKEN
    return await _get(client, f"{BINANCE_PROXY}/{path}", params)


# ── Individual feature fetchers ───────────────────────────────────────────────
async def _fetch_s1_coinbase(client, symbol: str, cutoff_ts: float) -> tuple[float, float]:
    """1-min Coinbase candle momentum at cutoff. Returns (momentum_pct, conf)."""
    pair = _CB_PAIRS.get(symbol)
    if not pair:
        return 0.0, 0.0
    try:
        end = int(cutoff_ts)
        start = end - 120
        r = await _get(client, f"https://api.exchange.coinbase.com/products/{pair}/candles",
                       {"granularity": 60, "start": start, "end": end})
        if not r or len(r) < 2:
            return 0.0, 0.0
        # Coinbase candles: [timestamp, low, high, open, close, volume]
        candles = sorted(r, key=lambda x: x[0])
        prev_close = candles[-2][4]
        last_close = candles[-1][4]
        if prev_close <= 0:
            return 0.0, 0.0
        mom = (last_close - prev_close) / prev_close
        conf = min(0.75, 0.55 + abs(mom) * 20)
        return mom, conf
    except Exception:
        return 0.0, 0.0


async def _fetch_s3_funding(client, symbol: str) -> tuple[float, float]:
    """OKX funding rate. Returns (rate, conf)."""
    inst = _OKX_PAIRS.get(symbol)
    if not inst:
        return 0.0, 0.0
    r = await _get(client, f"{OKX_BASE}/api/v5/public/funding-rate",
                   {"instId": inst})
    try:
        rate = float(r["data"][0]["fundingRate"])
        conf = min(0.72, 0.57 + abs(rate) * 500)
        return rate, conf
    except Exception:
        return 0.0, 0.0


async def _fetch_bn_momentum(client, symbol: str, cutoff_ts: float) -> tuple[float, float]:
    """Binance 1-min and 5-min momentum via Frankfurt proxy."""
    pair = _BN_PAIRS.get(symbol)
    if not pair:
        return 0.0, 0.0
    end_ms = int(cutoff_ts * 1000)
    start_ms = end_ms - 6 * 60 * 1000  # 6 minutes back
    r = await _binance(client, "api/v3/klines",
                       {"symbol": pair, "interval": "1m",
                        "startTime": start_ms, "endTime": end_ms, "limit": 10})
    if not r or len(r) < 2:
        return 0.0, 0.0
    try:
        # kline: [open_time, open, high, low, close, volume, ...]
        closes = [float(k[4]) for k in r]
        mom_1m = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0.0
        mom_5m = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] > 0 else 0.0
        return mom_1m, mom_5m
    except Exception:
        return 0.0, 0.0


async def _fetch_bn_volume_ratio(client, symbol: str, cutoff_ts: float) -> float:
    """Current 1-min volume vs 20-min average. >1 = elevated activity."""
    pair = _BN_PAIRS.get(symbol)
    if not pair:
        return 1.0
    end_ms = int(cutoff_ts * 1000)
    start_ms = end_ms - 25 * 60 * 1000
    r = await _binance(client, "api/v3/klines",
                       {"symbol": pair, "interval": "1m",
                        "startTime": start_ms, "endTime": end_ms, "limit": 25})
    if not r or len(r) < 5:
        return 1.0
    try:
        vols = [float(k[5]) for k in r]
        avg = sum(vols[:-1]) / len(vols[:-1])
        return vols[-1] / avg if avg > 0 else 1.0
    except Exception:
        return 1.0


async def _fetch_bn_cvd(client, symbol: str, cutoff_ts: float) -> float:
    """
    Cumulative Volume Delta (1-min window) via Binance aggTrades.
    CVD = sum(taker_buy_vol) - sum(taker_sell_vol).
    Positive = net buying pressure.
    """
    pair = _BN_PAIRS.get(symbol)
    if not pair:
        return 0.0
    end_ms   = int(cutoff_ts * 1000)
    start_ms = end_ms - 60 * 1000
    r = await _binance(client, "api/v3/aggTrades",
                       {"symbol": pair, "startTime": start_ms,
                        "endTime": end_ms, "limit": 1000})
    if not r:
        return 0.0
    try:
        buy_vol  = sum(float(t["q"]) for t in r if not t["m"])  # m=True → maker=sell
        sell_vol = sum(float(t["q"]) for t in r if t["m"])
        total    = buy_vol + sell_vol
        return (buy_vol - sell_vol) / total if total > 0 else 0.0  # normalised [-1, 1]
    except Exception:
        return 0.0


async def _fetch_bn_oi_change(client, symbol: str, cutoff_ts: float) -> float:
    """OI % change over last 5 minutes via Binance futures."""
    pair = _BN_PAIRS.get(symbol)
    if not pair:
        return 0.0
    r = await _binance(client, "fapi/v1/openInterestHist",
                       {"symbol": pair, "period": "5m", "limit": 2})
    if not r or len(r) < 2:
        return 0.0
    try:
        prev = float(r[-2]["sumOpenInterestValue"])
        curr = float(r[-1]["sumOpenInterestValue"])
        return (curr - prev) / prev if prev > 0 else 0.0
    except Exception:
        return 0.0


async def _fetch_s9_premium(client, symbol: str) -> float:
    """Coinbase vs Kraken price premium."""
    _KRAKEN = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD",
               "DOGE": "XDGEZUSD", "XRP": "XXRPZUSD"}
    cb_pair = _CB_PAIRS.get(symbol)
    kr_pair = _KRAKEN.get(symbol)
    if not cb_pair or not kr_pair:
        return 0.0
    cb_r, kr_r = await asyncio.gather(
        _get(client, f"https://api.exchange.coinbase.com/products/{cb_pair}/ticker"),
        _get(client, f"https://api.kraken.com/0/public/Ticker",
             {"pair": kr_pair}),
        return_exceptions=True
    )
    try:
        cb_price = float(cb_r["price"])
        kr_price = float(list(kr_r["result"].values())[0]["c"][0])
        return (cb_price - kr_price) / kr_price
    except Exception:
        return 0.0


async def _fetch_s10_basis(client, symbol: str) -> float:
    """OKX perp/spot basis."""
    inst = _OKX_PAIRS.get(symbol)
    cb_pair = _CB_PAIRS.get(symbol)
    if not inst or not cb_pair:
        return 0.0
    perp_r, spot_r = await asyncio.gather(
        _get(client, f"{OKX_BASE}/api/v5/market/ticker", {"instId": inst}),
        _get(client, f"https://api.exchange.coinbase.com/products/{cb_pair}/ticker"),
        return_exceptions=True
    )
    try:
        perp_price = float(perp_r["data"][0]["last"])
        spot_price = float(spot_r["price"])
        return (perp_price - spot_price) / spot_price
    except Exception:
        return 0.0


# ── Main feature builder ──────────────────────────────────────────────────────
async def build_features(
    symbol: str,
    direction: str,
    start_ts: float,
    end_ts: float,
    ask_price: float,
    condition_id: str = "",
    outcome: Optional[int] = None,
) -> MarketFeatures:
    """
    Build full feature vector for one market at entry time (start_ts).
    All data fetched with cutoff = start_ts (no lookahead).
    """
    import datetime as dt
    feat = MarketFeatures(
        symbol=symbol,
        direction=direction,
        duration_secs=int(end_ts - start_ts),
        secs_remaining=end_ts - start_ts,
        ask_price=ask_price,
        condition_id=condition_id,
        outcome=outcome,
    )
    utc = dt.datetime.utcfromtimestamp(start_ts)
    feat.hour_utc    = utc.hour
    feat.minute_utc  = utc.minute
    feat.day_of_week = utc.weekday()

    dsign = DIRECTION_SIGN.get(direction, 1)

    async with httpx.AsyncClient(timeout=10) as client:
        (
            (s1_mom, s1_conf),
            (s3_rate, s3_conf),
            (bn_mom_1m, bn_mom_5m),
            bn_vol_ratio,
            bn_cvd,
            bn_oi,
            s9_prem,
            s10_basis,
        ) = await asyncio.gather(
            _fetch_s1_coinbase(client, symbol, start_ts),
            _fetch_s3_funding(client, symbol),
            _fetch_bn_momentum(client, symbol, start_ts),
            _fetch_bn_volume_ratio(client, symbol, start_ts),
            _fetch_bn_cvd(client, symbol, start_ts),
            _fetch_bn_oi_change(client, symbol, start_ts),
            _fetch_s9_premium(client, symbol),
            _fetch_s10_basis(client, symbol),
        )

    feat.s1_momentum    = s1_mom * dsign   # positive = with direction
    feat.s1_conf        = s1_conf
    feat.s3_funding     = s3_rate * dsign  # positive funding + Up = good
    feat.s3_conf        = s3_conf
    feat.bn_momentum_1m = bn_mom_1m * dsign
    feat.bn_momentum_5m = bn_mom_5m * dsign
    feat.bn_volume_ratio = bn_vol_ratio
    feat.bn_cvd_1m      = bn_cvd * dsign
    feat.bn_oi_change   = bn_oi
    feat.s9_premium     = s9_prem * dsign
    feat.s10_basis      = s10_basis * dsign

    return feat


def features_to_dict(f: MarketFeatures) -> dict:
    """Convert to flat dict for pandas / XGBoost."""
    return {
        "symbol":          f.symbol,
        "direction":       f.direction,
        "duration_secs":   f.duration_secs,
        "hour_utc":        f.hour_utc,
        "minute_utc":      f.minute_utc,
        "day_of_week":     f.day_of_week,
        "ask_price":       f.ask_price,
        "s1_momentum":     f.s1_momentum,
        "s1_conf":         f.s1_conf,
        "s3_funding":      f.s3_funding,
        "s3_conf":         f.s3_conf,
        "s9_premium":      f.s9_premium,
        "s10_basis":       f.s10_basis,
        "bn_momentum_1m":  f.bn_momentum_1m,
        "bn_momentum_5m":  f.bn_momentum_5m,
        "bn_volume_ratio": f.bn_volume_ratio,
        "bn_cvd_1m":       f.bn_cvd_1m,
        "bn_oi_change":    f.bn_oi_change,
        "drift_pct":       f.drift_pct,
        "drift_signed":    f.drift_signed,
        "ev":              f.ev,
        "p_win":           f.p_win,
        "outcome":         f.outcome,
        "condition_id":    f.condition_id,
    }


if __name__ == "__main__":
    import json
    async def test():
        import time
        feat = await build_features(
            symbol="BTC", direction="Up",
            start_ts=time.time() - 60,
            end_ts=time.time() + 240,
            ask_price=0.52,
        )
        print(json.dumps(features_to_dict(feat), indent=2))
    asyncio.run(test())
