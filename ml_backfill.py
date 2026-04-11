"""
ml_backfill.py — Compute signal features for all historical markets from local price cache.

Reads price_cache/ (output of ml_prefetch.py) and markets_updown.parquet.
Computes all features locally — zero API calls. Writes training_data.jsonl.

Run ml_prefetch.py first.

Usage:
  python ml_backfill.py [--markets C:/tmp/ml_data/markets_updown.parquet]
                        [--cache   C:/tmp/ml_data/price_cache]
                        [--out     C:/tmp/ml_data/training_data.jsonl]
                        [--workers 8]
                        [--resume]
"""
import argparse
import json
import math
import os
import time
from pathlib import Path
from multiprocessing import Pool, cpu_count

import pandas as pd

DIRECTION_SIGN = {"Up": 1, "Down": -1}


# ── Price cache loader ────────────────────────────────────────────────────────

class PriceCache:
    """Loads price data from parquet files, provides fast timestamp lookups."""

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self._cb      = {}
        self._bn      = {}
        self._okx     = {}
        self._funding = {}

    def _load(self, store, prefix, symbol):
        if symbol not in store:
            p = self.cache_dir / f"{prefix}_{symbol}.parquet"
            if p.exists():
                df = pd.read_parquet(p).set_index("ts").sort_index()
                store[symbol] = df
            else:
                store[symbol] = None
        return store[symbol]

    def _candle_at(self, store, prefix, symbol, ts, col="close"):
        df = self._load(store, prefix, symbol)
        if df is None or df.empty:
            return None
        idx = df.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        return df.iloc[idx][col]

    def _candles_before(self, store, prefix, symbol, ts, n):
        df = self._load(store, prefix, symbol)
        if df is None or df.empty:
            return []
        idx = df.index.searchsorted(ts, side="right")
        start = max(0, idx - n)
        return df.iloc[start:idx].to_dict("records")

    def cb_close(self, symbol, ts):
        return self._candle_at(self._cb, "cb", symbol, ts, "close")

    def cb_open(self, symbol, ts):
        return self._candle_at(self._cb, "cb", symbol, ts, "open")

    def cb_candles(self, symbol, ts, n):
        return self._candles_before(self._cb, "cb", symbol, ts, n)

    def bn_close(self, symbol, ts):
        return self._candle_at(self._bn, "bn", symbol, ts, "close")

    def bn_candles(self, symbol, ts, n):
        return self._candles_before(self._bn, "bn", symbol, ts, n)

    def okx_close(self, symbol, ts):
        return self._candle_at(self._okx, "okx_perp", symbol, ts, "close")

    def okx_funding(self, symbol, ts):
        df = self._load(self._funding, "okx_funding", symbol)
        if df is None or df.empty:
            return None
        idx = df.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        return df.iloc[idx]["rate"]

    def warm(self, symbols):
        """Pre-load all cache files for given symbols."""
        for sym in symbols:
            self._load(self._cb,      "cb",          sym)
            self._load(self._bn,      "bn",          sym)
            self._load(self._okx,     "okx_perp",    sym)
            self._load(self._funding, "okx_funding", sym)


# ── Feature computers ─────────────────────────────────────────────────────────

def feat_s1_momentum(cache, symbol, ts):
    candles = cache.cb_candles(symbol, ts, 3)
    if len(candles) < 2:
        return {"s1_momentum": 0.0, "s1_conf": 0.0}
    prev = candles[-2]["close"]
    last = candles[-1]["close"]
    mom  = (last - prev) / prev if prev > 0 else 0.0
    conf = min(0.75, 0.55 + abs(mom) * 20)
    return {"s1_momentum": mom, "s1_conf": conf}


def feat_s3_funding(cache, symbol, ts):
    rate = cache.okx_funding(symbol, ts)
    if rate is None:
        return {"s3_funding": 0.0, "s3_conf": 0.0}
    conf = min(0.72, 0.57 + abs(rate) * 500)
    return {"s3_funding": rate, "s3_conf": conf}


def feat_coinbase_price(cache, symbol, ts):
    candles = cache.cb_candles(symbol, ts, 7)
    if not candles:
        return {"cb_price": 0.0, "cb_mom_5m": 0.0}
    price    = candles[-1]["close"]
    price_5m = candles[-6]["close"] if len(candles) >= 6 else price
    mom_5m   = (price - price_5m) / price_5m if price_5m > 0 else 0.0
    return {"cb_price": price, "cb_mom_5m": mom_5m}


def feat_bn_momentum(cache, symbol, ts):
    """1-min and 5-min BN momentum."""
    candles = cache.bn_candles(symbol, ts, 8)
    if len(candles) < 2:
        return {"bn_momentum_1m": 0.0, "bn_momentum_5m": 0.0}
    closes = [c["close"] for c in candles]
    m1 = (closes[-1] - closes[-2]) / closes[-2] if closes[-2] > 0 else 0.0
    m5 = (closes[-1] - closes[-6]) / closes[-6] if len(closes) >= 6 and closes[-6] > 0 else 0.0
    return {"bn_momentum_1m": m1, "bn_momentum_5m": m5}


def feat_bn_momentum_long(cache, symbol, ts):
    """15-min, 30-min, 60-min BN momentum."""
    candles = cache.bn_candles(symbol, ts, 62)
    if len(candles) < 2:
        return {"bn_momentum_15m": 0.0, "bn_momentum_30m": 0.0, "bn_momentum_60m": 0.0}
    closes = [c["close"] for c in candles]
    curr = closes[-1]
    m15 = (curr - closes[-16]) / closes[-16] if len(closes) >= 16 and closes[-16] > 0 else 0.0
    m30 = (curr - closes[-31]) / closes[-31] if len(closes) >= 31 and closes[-31] > 0 else 0.0
    m60 = (curr - closes[-61]) / closes[-61] if len(closes) >= 61 and closes[-61] > 0 else 0.0
    return {"bn_momentum_15m": m15, "bn_momentum_30m": m30, "bn_momentum_60m": m60}


def feat_bn_volume_ratio(cache, symbol, ts):
    candles = cache.bn_candles(symbol, ts, 25)
    if len(candles) < 3:
        return {"bn_volume_ratio": 1.0}
    vols = [c["volume"] for c in candles]
    avg  = sum(vols[:-1]) / len(vols[:-1])
    return {"bn_volume_ratio": vols[-1] / avg if avg > 0 else 1.0}


def feat_realized_vol(cache, symbol, ts):
    """Realized volatility from last 30 BN 1-min candles."""
    candles = cache.bn_candles(symbol, ts, 31)
    if len(candles) < 5:
        return {"realized_vol_30m": 0.02}
    closes  = [c["close"] for c in candles]
    returns = [(closes[i] - closes[i-1]) / closes[i-1]
               for i in range(1, len(closes)) if closes[i-1] > 0]
    if len(returns) < 2:
        return {"realized_vol_30m": 0.02}
    mean = sum(returns) / len(returns)
    vol  = math.sqrt(sum((r - mean)**2 for r in returns) / (len(returns) - 1))
    return {"realized_vol_30m": vol}


def feat_price_vs_ma(cache, symbol, ts):
    """Price deviation from 20-min and 60-min BN moving averages."""
    candles = cache.bn_candles(symbol, ts, 61)
    if not candles:
        return {"price_vs_ma20": 0.0, "price_vs_ma60": 0.0}
    closes = [c["close"] for c in candles]
    curr   = closes[-1]
    ma20   = sum(closes[-20:]) / len(closes[-20:]) if len(closes) >= 20 else curr
    ma60   = sum(closes) / len(closes)
    return {
        "price_vs_ma20": (curr - ma20) / ma20 if ma20 > 0 else 0.0,
        "price_vs_ma60": (curr - ma60) / ma60 if ma60 > 0 else 0.0,
    }


def feat_s10_basis(cache, symbol, ts):
    perp = cache.okx_close(symbol, ts)
    spot = cache.cb_close(symbol, ts)
    if perp is None or spot is None or spot == 0:
        return {"s10_basis": 0.0}
    return {"s10_basis": (perp - spot) / spot}


def feat_drift(cache, symbol, ts, end_ts, ask_price, direction):
    candles = cache.cb_candles(symbol, ts, 4)
    if not candles:
        return {"drift_pct": 0.0, "drift_signed": 0.0, "ev": 0.0, "p_win": 0.5}
    try:
        ref_price  = candles[0]["open"]
        curr_price = candles[-1]["close"]
        if ref_price <= 0:
            raise ValueError
        drift_pct    = (curr_price - ref_price) / ref_price
        dsign        = DIRECTION_SIGN.get(direction, 1)
        drift_signed = drift_pct * dsign

        returns = []
        for i in range(1, len(candles)):
            p0 = candles[i-1]["close"]
            p1 = candles[i]["close"]
            if p0 > 0:
                returns.append((p1 - p0) / p0)

        if len(returns) >= 2:
            mean      = sum(returns) / len(returns)
            std       = math.sqrt(sum((r - mean)**2 for r in returns) / (len(returns) - 1))
            vol_daily = std * math.sqrt(1440)
        else:
            vol_daily = 0.02

        tau       = max(1.0, end_ts - ts)
        sigma_tau = vol_daily * math.sqrt(tau / 86400.0)
        if sigma_tau > 0:
            z     = drift_signed / sigma_tau
            p_win = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
            p_win = max(0.01, min(0.99, p_win))
        else:
            p_win = 0.5

        ev = round(p_win * 0.99 - ask_price, 4)
        return {"drift_pct": drift_pct, "drift_signed": drift_signed,
                "ev": ev, "p_win": p_win}
    except Exception:
        return {"drift_pct": 0.0, "drift_signed": 0.0, "ev": 0.0, "p_win": 0.5}


# ── Per-market feature vector ─────────────────────────────────────────────────

DIRECTIONAL_FEATS = {
    "s1_momentum", "s3_funding", "cb_mom_5m",
    "bn_momentum_1m", "bn_momentum_5m",
    "bn_momentum_15m", "bn_momentum_30m", "bn_momentum_60m",
    "bn_cvd_1m", "s9_premium", "s10_basis",
    "drift_signed", "price_vs_ma20", "price_vs_ma60",
}

def build_features(cache, market):
    symbol        = market["symbol"]
    direction     = market["direction"]
    end_ts        = float(market["end_ts"])
    duration_secs = int(market.get("duration_secs", 300))
    window_start_ts = end_ts - duration_secs
    ask_price     = float(market.get("ask_price", 0.5))
    dsign         = DIRECTION_SIGN.get(direction, 1)

    import datetime as dt
    utc = dt.datetime.fromtimestamp(window_start_ts, dt.timezone.utc)

    feats = {}
    for fn in [feat_s1_momentum, feat_s3_funding, feat_coinbase_price,
               feat_bn_momentum, feat_bn_momentum_long, feat_bn_volume_ratio,
               feat_realized_vol, feat_price_vs_ma, feat_s10_basis]:
        feats.update(fn(cache, symbol, window_start_ts))

    feats.update(feat_drift(cache, symbol, window_start_ts, end_ts, ask_price, direction))

    row = {
        "condition_id":  market.get("condition_id", ""),
        "symbol":        symbol,
        "direction":     direction,
        "duration_secs": duration_secs,
        "hour_utc":      utc.hour,
        "minute_utc":    utc.minute,
        "day_of_week":   utc.weekday(),
        "ask_price":     ask_price,
        "outcome":       int(market["outcome"]),
        "s9_premium":    0.0,
        "bn_cvd_1m":     0.0,
    }

    for k, v in feats.items():
        row[k] = v * dsign if k in DIRECTIONAL_FEATS else v

    return row


# ── Multiprocessing worker ────────────────────────────────────────────────────

def _worker(args):
    """Process a chunk of markets. Called by each worker process."""
    markets_chunk, cache_dir, symbols = args
    cache = PriceCache(cache_dir)
    cache.warm(symbols)
    results = []
    for market in markets_chunk:
        try:
            results.append(build_features(cache, market))
        except Exception:
            pass
    return results


# ── Main ──────────────────────────────────────────────────────────────────────

def run_backfill(markets_path, cache_dir, out_path, resume=True, n_workers=None):
    if n_workers is None:
        n_workers = min(cpu_count(), 8)

    print(f"Loading markets from {markets_path}...")
    df = pd.read_parquet(markets_path)
    df = df[df["outcome"].notna() & df["start_ts"].notna() & df["end_ts"].notna()].copy()
    print(f"  {len(df):,} markets")

    done_cids = set()
    if resume and Path(out_path).exists():
        with open(out_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if d.get("condition_id"):
                        done_cids.add(d["condition_id"] + d.get("direction", ""))
                except Exception:
                    pass
        print(f"  Resuming — {len(done_cids):,} already done")

    markets = df[~(df["condition_id"] + df["direction"]).isin(done_cids)].to_dict("records")
    total   = len(markets)
    print(f"  {total:,} to process")
    symbols = df["symbol"].unique().tolist()

    print(f"\nUsing {n_workers} worker processes...")
    t0 = time.time()

    # Split into chunks — one per worker
    chunk_size = math.ceil(total / n_workers)
    chunks = [markets[i:i+chunk_size] for i in range(0, total, chunk_size)]
    args   = [(chunk, str(cache_dir), symbols) for chunk in chunks]

    all_rows = []
    with Pool(n_workers) as pool:
        for i, result in enumerate(pool.imap_unordered(_worker, args)):
            all_rows.extend(result)
            elapsed = time.time() - t0
            rate    = len(all_rows) / elapsed if elapsed > 0 else 0
            print(f"  Chunk {i+1}/{len(chunks)} done — {len(all_rows):,}/{total:,} rows  ({rate:.0f}/s)")

    # Write all results
    print(f"\nWriting {len(all_rows):,} rows to {out_path}...")
    with open(out_path, "a") as out_f:
        for row in all_rows:
            out_f.write(json.dumps(row) + "\n")

    elapsed = time.time() - t0
    print(f"Done in {elapsed/60:.1f} min. {len(all_rows):,} rows written.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets",  default="C:/tmp/ml_data/markets_updown.parquet")
    parser.add_argument("--cache",    default="C:/tmp/ml_data/price_cache")
    parser.add_argument("--out",      default="C:/tmp/ml_data/training_data.jsonl")
    parser.add_argument("--workers",  type=int, default=None)
    parser.add_argument("--resume",   action="store_true", default=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false")
    args = parser.parse_args()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    run_backfill(args.markets, args.cache, args.out, args.resume, args.workers)


if __name__ == "__main__":
    main()
