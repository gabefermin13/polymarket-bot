"""
ml_prefetch.py — Bulk-download historical price data for all symbols.

Downloads complete 1-min candle history + OKX funding rates for the date range
covered by markets_updown.parquet. Saves to C:/tmp/ml_data/price_cache/.

Run this BEFORE ml_backfill.py. Takes ~30 min. Safe to re-run (resumes).

Usage:
  python ml_prefetch.py [--markets C:/tmp/ml_data/markets_updown.parquet]
                        [--out C:/tmp/ml_data/price_cache]
"""
import argparse
import asyncio
import os
import time
from pathlib import Path

import httpx
import pandas as pd

BINANCE_PROXY = os.getenv("BINANCE_PROXY", "http://138.197.181.139:8081")
PROXY_TOKEN   = os.getenv("PROXY_TOKEN",   "poly_binance_proxy_2026")

_CB_EXCHANGE = "https://api.exchange.coinbase.com"
_OKX_BASE    = "https://www.okx.com"

_CB_PAIRS  = {"BTC": "BTC-USD",        "ETH": "ETH-USD",
              "DOGE": "DOGE-USD",       "XRP": "XRP-USD"}
_BN_PAIRS  = {"BTC": "BTCUSDT",        "ETH": "ETHUSDT",
              "DOGE": "DOGEUSDT",       "XRP": "XRPUSDT"}
_OKX_PERP  = {"BTC": "BTC-USDT-SWAP",  "ETH": "ETH-USDT-SWAP",
              "DOGE": "DOGE-USDT-SWAP", "XRP": "XRP-USDT-SWAP"}
_OKX_SPOT  = {"BTC": "BTC-USDT",       "ETH": "ETH-USDT",
              "DOGE": "DOGE-USDT",      "XRP": "XRP-USDT"}

# Conservative rate limits
_CB_SEM  = asyncio.Semaphore(8)   # Coinbase: 10/s safe
_BN_SEM  = asyncio.Semaphore(12)  # Binance: 20/s safe
_OKX_SEM = asyncio.Semaphore(4)   # OKX: conservative to avoid 429

CB_CANDLES_PER_REQ  = 300   # Coinbase max per request
BN_CANDLES_PER_REQ  = 1000  # Binance max per request
OKX_CANDLES_PER_REQ = 100   # OKX max per request (history-candles)
OKX_FUNDING_PER_REQ = 100   # OKX funding rate history per request


async def _get(client, url, params=None, timeout=15.0):
    try:
        r = await client.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        print(f"  HTTP {r.status_code}: {url}")
    except Exception as e:
        print(f"  Error: {e} — {url}")
    return None


async def _cb(client, path, params=None):
    async with _CB_SEM:
        return await _get(client, f"{_CB_EXCHANGE}{path}", params)


async def _bn(client, path, params):
    async with _BN_SEM:
        p = dict(params)
        p["token"] = PROXY_TOKEN
        return await _get(client, f"{BINANCE_PROXY}/{path}", p)


async def _okx(client, path, params=None):
    async with _OKX_SEM:
        return await _get(client, f"{_OKX_BASE}{path}", params)


# ── Coinbase 1-min candles ────────────────────────────────────────────────────

async def fetch_cb_candles(client, symbol, start_ts, end_ts, out_path):
    """Fetch all 1-min CB candles for symbol between start_ts and end_ts."""
    pair = _CB_PAIRS[symbol]
    step = CB_CANDLES_PER_REQ * 60  # seconds per batch
    all_rows = []

    batches = list(range(int(start_ts), int(end_ts), step))
    print(f"  CB {symbol}: {len(batches)} batches...")

    tasks = []
    for batch_start in batches:
        batch_end = min(batch_start + step, int(end_ts))
        tasks.append(_cb(client, f"/products/{pair}/candles",
                         {"granularity": 60, "start": batch_start, "end": batch_end}))

    results = await asyncio.gather(*tasks)
    for r in results:
        if r:
            for c in r:
                all_rows.append({"ts": int(c[0]), "open": float(c[3]),
                                  "high": float(c[2]), "low": float(c[1]),
                                  "close": float(c[4]), "volume": float(c[5])})

    if all_rows:
        df = pd.DataFrame(all_rows).drop_duplicates("ts").sort_values("ts")
        df.to_parquet(out_path, index=False)
        print(f"  CB {symbol}: {len(df):,} candles saved")
    else:
        print(f"  CB {symbol}: no data!")


# ── Binance 1-min klines ──────────────────────────────────────────────────────

async def fetch_bn_klines(client, symbol, start_ts, end_ts, out_path):
    """Fetch all 1-min Binance klines via Frankfurt proxy."""
    pair = _BN_PAIRS[symbol]
    step_ms = BN_CANDLES_PER_REQ * 60 * 1000
    start_ms = int(start_ts * 1000)
    end_ms   = int(end_ts   * 1000)
    all_rows = []

    batches = list(range(start_ms, end_ms, step_ms))
    print(f"  BN {symbol}: {len(batches)} batches...")

    tasks = []
    for bs in batches:
        be = min(bs + step_ms, end_ms)
        tasks.append(_bn(client, "api/v3/klines",
                         {"symbol": pair, "interval": "1m",
                          "startTime": bs, "endTime": be, "limit": BN_CANDLES_PER_REQ}))

    results = await asyncio.gather(*tasks)
    for r in results:
        if r:
            for k in r:
                all_rows.append({"ts": int(k[0]) // 1000, "open": float(k[1]),
                                  "high": float(k[2]), "low": float(k[3]),
                                  "close": float(k[4]), "volume": float(k[5])})

    if all_rows:
        df = pd.DataFrame(all_rows).drop_duplicates("ts").sort_values("ts")
        df.to_parquet(out_path, index=False)
        print(f"  BN {symbol}: {len(df):,} candles saved")
    else:
        print(f"  BN {symbol}: no data!")


# ── OKX perp 1-min candles ────────────────────────────────────────────────────

async def fetch_okx_perp(client, symbol, start_ts, end_ts, out_path):
    """Fetch OKX USDT-SWAP 5-min candles (history-candles endpoint).
    5-min resolution is sufficient for S10 basis — 25x fewer requests than 1-min."""
    inst = _OKX_PERP[symbol]
    bar_secs = 5 * 60
    step_ms  = OKX_CANDLES_PER_REQ * bar_secs * 1000
    start_ms = int(start_ts * 1000)
    end_ms   = int(end_ts   * 1000)
    all_rows = []

    batches = list(range(start_ms, end_ms, step_ms))
    print(f"  OKX perp {symbol}: {len(batches)} batches (5-min bars)...")

    tasks = []
    for bs in batches:
        be = min(bs + step_ms, end_ms)
        tasks.append(_okx(client, "/api/v5/market/history-candles",
                          {"instId": inst, "bar": "5m",
                           "after": str(bs), "before": str(be),
                           "limit": str(OKX_CANDLES_PER_REQ)}))

    results = await asyncio.gather(*tasks)
    for r in results:
        if r and r.get("data"):
            for c in r["data"]:
                all_rows.append({"ts": int(c[0]) // 1000, "open": float(c[1]),
                                  "high": float(c[2]), "low": float(c[3]),
                                  "close": float(c[4]), "volume": float(c[5])})

    if all_rows:
        df = pd.DataFrame(all_rows).drop_duplicates("ts").sort_values("ts")
        df.to_parquet(out_path, index=False)
        print(f"  OKX perp {symbol}: {len(df):,} candles saved")
    else:
        print(f"  OKX perp {symbol}: no data!")


# ── OKX funding rate history ──────────────────────────────────────────────────

async def fetch_okx_funding(client, symbol, start_ts, end_ts, out_path):
    """Fetch OKX funding rate history (settled every 8h)."""
    inst = _OKX_PERP[symbol]
    all_rows = []
    # Paginate: use before param (ms timestamp), walk backwards
    before_ms = int(end_ts * 1000) + 1

    print(f"  OKX funding {symbol}: paginating...")
    while True:
        r = await _okx(client, "/api/v5/public/funding-rate-history",
                       {"instId": inst, "before": str(before_ms),
                        "limit": str(OKX_FUNDING_PER_REQ)})
        if not r or not r.get("data"):
            break
        data = r["data"]
        for d in data:
            ts = int(d["fundingTime"]) // 1000
            if ts < start_ts:
                break
            all_rows.append({"ts": ts, "rate": float(d["fundingRate"])})
        # If oldest record is before start_ts, we're done
        oldest = int(data[-1]["fundingTime"]) // 1000
        if oldest <= start_ts or len(data) < OKX_FUNDING_PER_REQ:
            break
        before_ms = int(data[-1]["fundingTime"])

    if all_rows:
        df = pd.DataFrame(all_rows).drop_duplicates("ts").sort_values("ts")
        df.to_parquet(out_path, index=False)
        print(f"  OKX funding {symbol}: {len(df):,} records saved")
    else:
        print(f"  OKX funding {symbol}: no data!")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_prefetch(markets_path, out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(markets_path)
    # Get date range per symbol
    symbols = df["symbol"].unique().tolist()
    global_start = df["start_ts"].min() - 600   # 10 min buffer
    global_end   = df["end_ts"].max()   + 600

    print(f"Date range: {pd.Timestamp(global_start, unit='s')} to {pd.Timestamp(global_end, unit='s')}")
    print(f"Symbols: {symbols}")
    print()

    limits = httpx.Limits(max_connections=40, max_keepalive_connections=20)
    async with httpx.AsyncClient(timeout=20, limits=limits) as client:

        # Per-symbol fetches — run all symbols in parallel
        tasks = []
        for sym in symbols:
            s_df = df[df["symbol"] == sym]
            s_start = s_df["start_ts"].min() - 600
            s_end   = s_df["end_ts"].max()   + 600

            cb_path      = out / f"cb_{sym}.parquet"
            bn_path      = out / f"bn_{sym}.parquet"
            okx_path     = out / f"okx_perp_{sym}.parquet"
            funding_path = out / f"okx_funding_{sym}.parquet"

            if not cb_path.exists():
                tasks.append(fetch_cb_candles(client, sym, s_start, s_end, cb_path))
            else:
                print(f"  CB {sym}: already cached, skipping")

            if not bn_path.exists():
                tasks.append(fetch_bn_klines(client, sym, s_start, s_end, bn_path))
            else:
                print(f"  BN {sym}: already cached, skipping")

            if not okx_path.exists():
                tasks.append(fetch_okx_perp(client, sym, s_start, s_end, okx_path))
            else:
                print(f"  OKX perp {sym}: already cached, skipping")

            if not funding_path.exists():
                tasks.append(fetch_okx_funding(client, sym, s_start, s_end, funding_path))
            else:
                print(f"  OKX funding {sym}: already cached, skipping")

        if tasks:
            print(f"Running {len(tasks)} fetch tasks in parallel...")
            t0 = time.time()
            await asyncio.gather(*tasks)
            print(f"\nAll fetches done in {(time.time()-t0)/60:.1f} min")
        else:
            print("All data already cached.")

    # Verify
    print("\nCache summary:")
    for sym in symbols:
        for prefix in ["cb", "bn", "okx_perp", "okx_funding"]:
            p = out / f"{prefix}_{sym}.parquet"
            if p.exists():
                rows = len(pd.read_parquet(p))
                print(f"  {prefix}_{sym}: {rows:,} rows")
            else:
                print(f"  {prefix}_{sym}: MISSING")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="C:/tmp/ml_data/markets_updown.parquet")
    parser.add_argument("--out",     default="C:/tmp/ml_data/price_cache")
    args = parser.parse_args()
    asyncio.run(run_prefetch(args.markets, args.out))


if __name__ == "__main__":
    main()
