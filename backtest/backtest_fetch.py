"""
backtest_fetch.py — Pull all resolved Polymarket BTC/ETH/DOGE Up-or-Down markets.
Output: backtest/markets.jsonl
Usage: python backtest_fetch.py [--out backtest/markets.jsonl]
"""
import argparse, asyncio, json, time
from pathlib import Path
import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
ASSETS = {"BTC", "ETH", "DOGE"}
MIN_MARKETS = 300


def parse_asset(question: str) -> str | None:
    q = question.upper()
    for a in ASSETS:
        if a in q:
            return a
    return None


def parse_outcome(outcomes: list[str], prices: list[str]) -> str | None:
    try:
        floats = [float(p) for p in prices]
    except (ValueError, TypeError):
        return None
    for i, p in enumerate(floats):
        if p >= 0.90:
            return outcomes[i]
    return None


def parse_duration(start: float, end: float) -> str | None:
    diff = end - start
    if 240 <= diff <= 360:
        return "5min"
    if 780 <= diff <= 1020:
        return "15min"
    return None


def compute_entry_ts(end_time: float, duration_type: str) -> float:
    return end_time - (750 if duration_type == "15min" else 75)


def _iso_to_ts(iso: str) -> float:
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _parse_ts(val) -> float:
    if isinstance(val, str) and "T" in val:
        return _iso_to_ts(val)
    try:
        return float(val or 0)
    except (ValueError, TypeError):
        return 0.0


async def fetch_markets(out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with httpx.AsyncClient(timeout=30) as client:
        offset = 0
        with open(out_path, "w", encoding="utf-8") as f:
            while True:
                resp = await client.get(f"{GAMMA_API}/markets",
                    params={"closed": "true", "limit": 100, "offset": offset})
                resp.raise_for_status()
                markets = resp.json()
                if not markets:
                    break
                for m in markets:
                    question = m.get("question", "")
                    asset = parse_asset(question)
                    if not asset:
                        continue
                    ql = question.lower()
                    if "up or down" not in ql and "higher or lower" not in ql:
                        continue
                    outcomes = m.get("outcomes", [])
                    prices   = m.get("outcomePrices", [])
                    if len(outcomes) != 2 or len(prices) != 2:
                        continue
                    outcome = parse_outcome(outcomes, prices)
                    if not outcome:
                        continue
                    start_time = _parse_ts(m.get("startDateIso") or m.get("startDate", ""))
                    end_time   = _parse_ts(m.get("endDateIso")   or m.get("endDate",   ""))
                    if not start_time or not end_time:
                        continue
                    duration_type = parse_duration(start_time, end_time)
                    if not duration_type:
                        continue
                    tokens     = m.get("tokens", [])
                    up_token   = next((t["token_id"] for t in tokens if t.get("outcome","").lower()=="up"),   "")
                    down_token = next((t["token_id"] for t in tokens if t.get("outcome","").lower()=="down"), "")
                    if not up_token or not down_token:
                        continue
                    f.write(json.dumps({
                        "market_id":     m.get("conditionId", ""),
                        "asset":         asset,
                        "outcome":       outcome,
                        "start_time":    start_time,
                        "end_time":      end_time,
                        "duration_type": duration_type,
                        "entry_ts":      compute_entry_ts(end_time, duration_type),
                        "up_token_id":   up_token,
                        "down_token_id": down_token,
                    }) + "\n")
                    written += 1
                offset += 100
                if len(markets) < 100:
                    break
                await asyncio.sleep(0.2)
    return written


async def main(out_path: Path):
    print(f"Fetching resolved markets → {out_path}")
    t0 = time.time()
    n = await fetch_markets(out_path)
    print(f"Written {n} markets in {time.time()-t0:.1f}s")
    if n < MIN_MARKETS:
        print(f"WARNING: only {n} markets — below {MIN_MARKETS} minimum.")
    else:
        print(f"OK: {n} markets >= {MIN_MARKETS} minimum threshold.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="backtest/markets.jsonl")
    args = parser.parse_args()
    asyncio.run(main(Path(args.out)))
