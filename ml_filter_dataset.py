"""
ml_filter_dataset.py — Extract crypto Up-or-Down markets from the 36GB parquet dataset.

Run ONCE after downloading and extracting data.tar.zst.
Outputs:
  C:/tmp/ml_data/markets_updown.parquet   — labeled markets (condition_id, symbol, direction, outcome, timestamps)
  C:/tmp/ml_data/trades_updown.parquet    — trades for those markets (for entry price / volume features)

Usage:
  python ml_filter_dataset.py --data-dir C:/tmp/data
"""
import argparse
import re
import os
import sys
import json
from pathlib import Path


def filter_one(args):
    fpath, cids_file, out_chunk = args
    try:
        import pandas as _pd
        token_ids = set(json.load(open(cids_file)))  # set of string token IDs
        df = _pd.read_parquet(fpath)
        if "maker_asset_id" not in df.columns:
            return 0
        # maker/taker already stored as strings in parquet — direct isin, no conversion
        filtered = df[df["maker_asset_id"].isin(token_ids) | df["taker_asset_id"].isin(token_ids)]
        if len(filtered) > 0:
            filtered.to_parquet(out_chunk, index=False)
            return len(filtered)
        return 0
    except Exception:
        return 0

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="C:/tmp/data",
                        help="Path to extracted data/ directory")
    parser.add_argument("--out-dir", default="C:/tmp/ml_data")
    args = parser.parse_args()

    try:
        import pandas as pd
    except ImportError:
        print("pip install pandas pyarrow")
        sys.exit(1)

    data_dir = Path(args.data_dir)
    out_dir  = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    pm_dir = data_dir / "polymarket"
    if not pm_dir.exists():
        print(f"ERROR: {pm_dir} not found. Check --data-dir.")
        sys.exit(1)

    # ── Load all markets parquet files ────────────────────────────────────────
    print("Loading Polymarket markets...")
    # Markets are in a subdirectory: polymarket/markets/markets_*.parquet
    markets_dir = pm_dir / "markets"
    market_files = list(markets_dir.glob("markets_*.parquet")) if markets_dir.exists() else []
    if not market_files:
        market_files = list(pm_dir.glob("markets_*.parquet"))
    if not market_files:
        market_files = list(pm_dir.glob("**/*.parquet"))
    # Exclude Mac metadata files (start with ._)
    market_files = [f for f in market_files if not f.name.startswith("._")]
    print(f"  Found {len(market_files)} market parquet files")

    dfs = []
    for f in market_files:
        df = pd.read_parquet(f)
        dfs.append(df)
    markets = pd.concat(dfs, ignore_index=True)
    print(f"  Total markets: {len(markets):,}")
    print(f"  Columns: {list(markets.columns)}")

    # ── Filter for Up-or-Down crypto markets ─────────────────────────────────
    SYMBOLS = {"BTC": "Bitcoin", "ETH": "Ethereum", "DOGE": "Dogecoin", "XRP": "XRP"}
    KEYWORDS = ["Up or Down", "up or down", "Up-or-Down"]

    def is_updown(q):
        if not isinstance(q, str):
            return False
        return any(kw in q for kw in KEYWORDS)

    updown = markets[markets["question"].apply(is_updown)].copy()
    print(f"\nUp-or-Down markets: {len(updown):,}")

    # Parse symbol from question
    def parse_symbol(q):
        q_lower = q.lower()
        if "bitcoin" in q_lower or "btc" in q_lower:
            return "BTC"
        if "ethereum" in q_lower or "eth" in q_lower:
            return "ETH"
        if "dogecoin" in q_lower or "doge" in q_lower:
            return "DOGE"
        if "xrp" in q_lower or "ripple" in q_lower:
            return "XRP"
        if "solana" in q_lower or "sol" in q_lower:
            return "SOL"
        return None

    updown["symbol"] = updown["question"].apply(parse_symbol)
    updown = updown[updown["symbol"].isin(["BTC", "ETH", "DOGE", "XRP"])].copy()
    print(f"BTC/ETH/DOGE/XRP only: {len(updown):,}")
    print(updown["symbol"].value_counts().to_string())

    # ── Parse outcome from outcome_prices ────────────────────────────────────
    import json as _json

    def parse_outcome(row):
        """
        outcome_prices is like '["1", "0"]' (YES won) or '["0", "1"]' (NO/Down won).
        outcomes is like '["Up", "Down"]' or '["Yes", "No"]'.
        Returns: 1 if Up/Yes won, 0 if Down/No won, None if unresolved.
        """
        try:
            prices = row.get("outcome_prices") or row.get("outcomePrices")
            if not prices:
                return None
            if isinstance(prices, str):
                prices = _json.loads(prices)
            prices = [float(p) for p in prices]
            if max(prices) < 0.99:
                return None  # unresolved / 50-50
            winner_idx = prices.index(max(prices))
            # Determine if winner_idx=0 means Up or Down
            outcomes = row.get("outcomes")
            if outcomes:
                if isinstance(outcomes, str):
                    outcomes = _json.loads(outcomes)
                winner_label = outcomes[winner_idx].lower()
                if winner_label in ("up", "yes", "higher"):
                    return 1
                elif winner_label in ("down", "no", "lower"):
                    return 0
            # Default: index 0 = Up (Polymarket convention for these markets)
            return 1 if winner_idx == 0 else 0
        except Exception:
            return None

    updown["outcome"] = updown.apply(parse_outcome, axis=1)
    resolved = updown[updown["outcome"].notna()].copy()
    print(f"\nResolved with clear outcome: {len(resolved):,}")
    print(resolved["outcome"].value_counts().to_string())
    print(resolved["symbol"].value_counts().to_string())

    # ── Parse timestamps ──────────────────────────────────────────────────────
    # end_date is the resolution time
    if "end_date" in resolved.columns:
        resolved["end_ts"] = pd.to_datetime(resolved["end_date"], utc=True, errors="coerce") \
                               .astype("int64") // 10**9
    if "created_at" in resolved.columns:
        resolved["start_ts"] = pd.to_datetime(resolved["created_at"], utc=True, errors="coerce") \
                                 .astype("int64") // 10**9

    # Parse window from title: "8:35PM-8:40PM ET" → duration_secs
    TIME_RE = re.compile(
        r"(\d{1,2}):(\d{2})(AM|PM)[–\-](\d{1,2}):(\d{2})(AM|PM)", re.IGNORECASE
    )
    def parse_duration(q):
        m = TIME_RE.search(str(q))
        if not m:
            return 300  # default 5-min
        h1, mi1, ap1, h2, mi2, ap2 = m.groups()
        def to_min(h, mi, ap):
            h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
            return h * 60 + int(mi)
        diff = to_min(h2, mi2, ap2) - to_min(h1, mi1, ap1)
        return abs(diff) * 60

    resolved["duration_secs"] = resolved["question"].apply(parse_duration)
    print("\nDuration distribution:")
    print(resolved["duration_secs"].value_counts().head(10).to_string())

    # ── Save filtered markets ─────────────────────────────────────────────────
    keep_cols = [c for c in [
        "condition_id", "id", "question", "symbol", "outcome",
        "start_ts", "end_ts", "duration_secs",
        "outcome_prices", "outcomes", "clob_token_ids",
        "volume", "liquidity", "active", "closed",
    ] if c in resolved.columns]

    out_markets = out_dir / "markets_updown.parquet"
    resolved[keep_cols].to_parquet(out_markets, index=False)
    print(f"\nSaved {len(resolved):,} markets to {out_markets}")

    # ── Load and filter trades using parallel workers ─────────────────────────
    from multiprocessing import Pool, cpu_count
    import tempfile
    import json as _json2

    # Extract token IDs from clob_token_ids (trades use token IDs, not condition_ids)
    token_ids = set()
    token_to_cid = {}
    for _, row in resolved.iterrows():
        cid = row.get("condition_id")
        tids_raw = row.get("clob_token_ids")
        if not tids_raw:
            continue
        try:
            tids = _json2.loads(tids_raw) if isinstance(tids_raw, str) else tids_raw
            for tid in tids:
                tid_str = str(tid)
                token_ids.add(tid_str)
                token_to_cid[tid_str] = cid
        except Exception:
            pass
    print(f"\nLoading trades for {len(token_ids):,} token IDs ({len(resolved):,} markets)...")

    trades_dir = pm_dir / "trades"
    trade_files = list(trades_dir.glob("trades_*.parquet")) if trades_dir.exists() else []
    if not trade_files:
        trade_files = list(pm_dir.glob("trades_*.parquet"))
    trade_files = [f for f in trade_files if not f.name.startswith("._")]
    print(f"  Found {len(trade_files)} trade parquet files")

    # Write token IDs to file so workers can load it
    cids_path = out_dir / "_target_cids.json"
    with open(cids_path, "w") as f:
        json.dump(list(token_ids), f)

    workers = min(cpu_count(), 16)
    print(f"  Using {workers} parallel workers...")

    out_trades = out_dir / "trades_updown.parquet"
    tmp_dir = out_dir / "_trade_chunks"
    tmp_dir.mkdir(exist_ok=True)

    args_list = [
        (str(f), str(cids_path), str(tmp_dir / f"{i}.parquet"))
        for i, f in enumerate(trade_files)
        if not (tmp_dir / f"{i}.parquet").exists()
    ]
    print(f"  {40454 - len(args_list)} already done, {len(args_list)} remaining")

    total_rows = 0
    done = 0
    with Pool(workers) as pool:
        for n in pool.imap_unordered(filter_one, args_list, chunksize=50):
            total_rows += n
            done += 1
            if done % 500 == 0 or done == len(args_list):
                print(f"  [{done}/{len(args_list)}] {total_rows:,} matching rows...")

    # Merge all chunks
    print("  Merging chunks...")
    chunk_files = [f for f in tmp_dir.glob("*.parquet")]
    if chunk_files:
        dfs = [pd.read_parquet(f) for f in chunk_files]
        trades = pd.concat(dfs, ignore_index=True)
        trades.to_parquet(out_trades, index=False)
        print(f"  Saved {len(trades):,} trade rows to {out_trades}")

        # ── Compute ask_price per market from earliest trade ──────────────────
        # maker_asset_id=0 means maker gives USDC → price = maker_amount/taker_amount
        # taker_asset_id=0 means taker gives USDC → price = taker_amount/maker_amount
        print("  Computing ask_price per market from trade data...")
        trades["maker_str"] = trades["maker_asset_id"].astype(str)
        trades["taker_str"] = trades["taker_asset_id"].astype(str)

        def compute_price(row):
            try:
                if str(row["maker_asset_id"]) == "0":
                    return row["maker_amount"] / row["taker_amount"]
                elif str(row["taker_asset_id"]) == "0":
                    return row["taker_amount"] / row["maker_amount"]
            except Exception:
                pass
            return None

        trades["price"] = trades.apply(compute_price, axis=1)
        trades["condition_id"] = trades["taker_str"].map(token_to_cid).fillna(
            trades["maker_str"].map(token_to_cid)
        )
        # Get earliest trade price per market (closest to market open = ask at entry)
        trades_valid = trades[trades["price"].notna() & trades["condition_id"].notna()].copy()
        if "timestamp" in trades_valid.columns:
            trades_valid = trades_valid.sort_values(["block_number", "log_index"])
        ask_prices = trades_valid.groupby("condition_id")["price"].first().reset_index()
        ask_prices.columns = ["condition_id", "ask_price"]
        print(f"  Got ask_price for {len(ask_prices):,} markets")

        # Join back to markets and save updated parquet
        markets_df = pd.read_parquet(out_markets)
        markets_df = markets_df.merge(ask_prices, on="condition_id", how="left")
        markets_df["ask_price"] = markets_df["ask_price"].fillna(0.5)
        markets_df.to_parquet(out_markets, index=False)
        print(f"  Updated markets_updown.parquet with ask_price column")
        print(f"  ask_price stats: mean={markets_df['ask_price'].mean():.3f} "
              f"min={markets_df['ask_price'].min():.3f} max={markets_df['ask_price'].max():.3f}")

        # Cleanup
        import shutil
        shutil.rmtree(tmp_dir)
        cids_path.unlink()
    else:
        print("  No matching trades found.")

    print("\nDone. Next step: run ml_backfill.py to reconstruct signal features.")


if __name__ == "__main__":
    main()
