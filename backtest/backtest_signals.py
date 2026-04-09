"""
backtest_signals.py — Reconstruct S1-S6 at historical entry timestamps.

Reads:  backtest/markets.jsonl
Writes: backtest/signal_rows.jsonl

Each output row: all market fields + signals {s1..s6: {direction, conf}}

Usage:
    python backtest_signals.py
    python backtest_signals.py --markets backtest/markets.jsonl --out backtest/signal_rows.jsonl
"""
import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

COINBASE_API  = "https://api.coinbase.com/api/v3/brokerage/market/products"
OKX_API       = "https://www.okx.com/api/v5/public"
POLY_DATA_API = "https://data-api.polymarket.com"
BATCH_SIZE    = 10
BATCH_SLEEP   = 1.0


def neutral_signal() -> tuple:
    return ("NEUTRAL", 0.5)


def classify_s1_from_candles(candles: list[dict]) -> tuple:
    """5 one-minute candles → momentum direction. Returns (direction, conf)."""
    if not candles:
        return neutral_signal()
    try:
        first_open = float(candles[0]["open"])
        last_close = float(candles[-1]["close"])
        if first_open == 0:
            return neutral_signal()
        pct = (last_close - first_open) / first_open
        if abs(pct) < 0.0005:
            return "NEUTRAL", 0.57
        conf = min(0.75, 0.55 + abs(pct) * 10)
        return ("UP" if pct > 0 else "DOWN", round(conf, 3))
    except (KeyError, ValueError, TypeError):
        return neutral_signal()


def classify_s3_from_funding(funding_rate: float) -> tuple:
    """Positive funding → longs paying → bullish → UP. |rate| < 0.0001 → NEUTRAL."""
    if abs(funding_rate) < 0.0001:
        return "NEUTRAL", 0.57
    conf = min(0.72, 0.57 + abs(funding_rate) * 500)
    return ("UP" if funding_rate > 0 else "DOWN", round(conf, 3))


def classify_s6_from_clob(net_volume: float) -> tuple:
    """net_volume = buy_vol - sell_vol for Up token. |net| < 100 USDC → BALANCED."""
    if abs(net_volume) < 100:
        return "BALANCED", 0.5
    conf = min(0.70, 0.56 + abs(net_volume) / 10000)
    return ("UP" if net_volume > 0 else "DOWN", round(conf, 3))


def aggregate_s5(results: list[tuple], primary_asset_result: tuple) -> tuple:
    """S5: >=2/3 of BTC/ETH/SOL agree AND primary is in majority → signal."""
    directions = [r[0] for r in results if r[0] not in ("NEUTRAL", "NONE", "BALANCED")]
    if len(directions) < 2:
        return neutral_signal()
    up_count   = directions.count("UP")
    down_count = directions.count("DOWN")
    total      = len(directions)
    primary_dir = primary_asset_result[0]
    if up_count >= 2 and primary_dir == "UP":
        return ("UP",   0.73 if up_count == total else 0.60)
    if down_count >= 2 and primary_dir == "DOWN":
        return ("DOWN", 0.73 if down_count == total else 0.60)
    return neutral_signal()


# ── Historical API fetchers ───────────────────────────────────────────────────

async def fetch_s1(client: httpx.AsyncClient, asset: str, entry_ts: float) -> tuple:
    try:
        resp = await client.get(
            f"{COINBASE_API}/{asset}-USD/candles",
            params={"start": str(int(entry_ts - 300)), "end": str(int(entry_ts)),
                    "granularity": "ONE_MINUTE"},
        )
        data = resp.json()
        candles = list(reversed(data.get("candles", [])))
        normalized = [{"open": float(c.get("open", 0)), "close": float(c.get("close", 0))}
                      for c in candles]
        return classify_s1_from_candles(normalized)
    except Exception:
        return neutral_signal()


async def fetch_s3(client: httpx.AsyncClient, asset: str, entry_ts: float) -> tuple:
    try:
        resp = await client.get(
            f"{OKX_API}/funding-rate-history",
            params={"instId": f"{asset}-USD-SWAP",
                    "before": str(int(entry_ts * 1000)),
                    "after":  str(int((entry_ts - 3600) * 1000)),
                    "limit":  "1"},
        )
        records = resp.json().get("data", [])
        if not records:
            return neutral_signal()
        return classify_s3_from_funding(float(records[0].get("fundingRate", 0)))
    except Exception:
        return neutral_signal()


async def fetch_s4(client: httpx.AsyncClient, asset: str, entry_ts: float) -> tuple:
    """Best-effort: OKX liquidation orders in 5-min window before entry."""
    try:
        resp = await client.get(
            f"{OKX_API}/liquidation-orders",
            params={"instType": "SWAP", "instId": f"{asset}-USDT-SWAP",
                    "before": str(int(entry_ts * 1000)),
                    "after":  str(int((entry_ts - 300) * 1000))},
        )
        records = resp.json().get("data", [])
        if not records:
            return neutral_signal()
        long_liq  = sum(float(r.get("sz", 0)) for r in records if r.get("side","").lower() == "buy")
        short_liq = sum(float(r.get("sz", 0)) for r in records if r.get("side","").lower() == "sell")
        threshold = 75000
        if long_liq > threshold and long_liq > short_liq * 2:
            return ("DOWN", 0.72)
        if short_liq > threshold and short_liq > long_liq * 2:
            return ("UP", 0.72)
        return neutral_signal()
    except Exception:
        return neutral_signal()


async def fetch_s6(client: httpx.AsyncClient, up_token_id: str, entry_ts: float) -> tuple:
    try:
        resp = await client.get(
            f"{POLY_DATA_API}/prices",
            params={"tokenId": up_token_id,
                    "startTs": str(int(entry_ts - 300)),
                    "endTs":   str(int(entry_ts)),
                    "interval": "1m"},
        )
        data = resp.json()
        prices = data if isinstance(data, list) else data.get("history", [])
        if not prices:
            return neutral_signal()
        net_vol = sum(
            float(p.get("volume", 0)) * (1 if float(p.get("price", 0.5)) > 0.5 else -1)
            for p in prices
        )
        return classify_s6_from_clob(net_vol)
    except Exception:
        return neutral_signal()


async def reconstruct_signals(client: httpx.AsyncClient, market: dict) -> dict:
    asset    = market["asset"]
    entry_ts = market["entry_ts"]
    up_token = market["up_token_id"]

    # S1 for primary + cross-assets (feeds S5)
    s1_btc, s1_eth, s1_sol = await asyncio.gather(
        fetch_s1(client, "BTC", entry_ts),
        fetch_s1(client, "ETH", entry_ts),
        fetch_s1(client, "SOL", entry_ts),
    )
    # DOGE has no Coinbase candles — use BTC as proxy
    s1_primary = {"BTC": s1_btc, "ETH": s1_eth, "DOGE": s1_btc}.get(asset, s1_btc)
    s5 = aggregate_s5([s1_btc, s1_eth, s1_sol], primary_asset_result=s1_primary)

    s3, s4, s6 = await asyncio.gather(
        fetch_s3(client, asset, entry_ts),
        fetch_s4(client, asset, entry_ts),
        fetch_s6(client, up_token, entry_ts),
    )

    return {
        "s1": {"direction": s1_primary[0], "conf": s1_primary[1]},
        "s2": {"direction": "NEUTRAL",     "conf": 0.5},   # not reconstructable
        "s3": {"direction": s3[0],         "conf": s3[1]},
        "s4": {"direction": s4[0],         "conf": s4[1]},
        "s5": {"direction": s5[0],         "conf": s5[1]},
        "s6": {"direction": s6[0],         "conf": s6[1]},
    }


async def process_batch(client: httpx.AsyncClient, batch: list[dict], out_file):
    results = await asyncio.gather(
        *[reconstruct_signals(client, m) for m in batch],
        return_exceptions=True,
    )
    for market, signals in zip(batch, results):
        if isinstance(signals, Exception):
            signals = {s: {"direction": "NEUTRAL", "conf": 0.5}
                       for s in ["s1", "s2", "s3", "s4", "s5", "s6"]}
        out_file.write(json.dumps({**market, "signals": signals}) + "\n")


async def main(markets_path: Path, out_path: Path):
    markets = [json.loads(l) for l in markets_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"Processing {len(markets)} markets → {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    written = 0
    async with httpx.AsyncClient(timeout=20) as client:
        with open(out_path, "w", encoding="utf-8") as f:
            for i in range(0, len(markets), BATCH_SIZE):
                batch = markets[i:i + BATCH_SIZE]
                await process_batch(client, batch, f)
                written += len(batch)
                print(f"  {written}/{len(markets)} ({written/len(markets)*100:.0f}%) — {time.time()-t0:.0f}s elapsed")
                if i + BATCH_SIZE < len(markets):
                    await asyncio.sleep(BATCH_SLEEP)
    print(f"Done. {written} rows written in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--markets", default="backtest/markets.jsonl")
    parser.add_argument("--out",     default="backtest/signal_rows.jsonl")
    args = parser.parse_args()
    asyncio.run(main(Path(args.markets), Path(args.out)))
