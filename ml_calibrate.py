"""
ml_calibrate.py — Continuous retraining cron for ML model.

Reads settled trades from D2 + W positions.jsonl, extracts features logged
in signals.jsonl, combines with historical training data (rolling 90-day window),
retrains the XGBoost model, saves to /root/shared_ml/.

Run every 30 min via cron:
  */30 * * * * python3 /root/ml_calibrate.py >> /tmp/ml_calibrate.log 2>&1

Requires: ml_train.py, /root/shared_ml/ directory
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
POSITIONS_FILES = [
    "/root/kalshiedge_dbot_d2/logs_d2/positions.jsonl",
    "/root/kalshiedge_whalebot/logs_w/positions.jsonl",
]
SIGNALS_FILES = [
    "/root/kalshiedge_dbot_d2/logs_d2/signals.jsonl",
    "/root/kalshiedge_whalebot/logs_w/signals.jsonl",
]
HISTORICAL_DATA  = "/root/shared_ml/training_data_historical.jsonl"
LIVE_DATA        = "/root/shared_ml/training_data_live.jsonl"
COMBINED_DATA    = "/root/shared_ml/training_data_combined.jsonl"
MODEL_DIR        = "/root/shared_ml"
ML_TRAIN_SCRIPT  = "/root/ml_train.py"
ROLLING_DAYS     = 90
MIN_LIVE_ROWS    = 50   # Don't retrain until we have at least this many live examples


# ── Extract live training rows from positions + signals ───────────────────────

def load_signals_index(signals_files):
    """Build index: (condition_id, direction) -> feature dict from signals.jsonl."""
    index = {}
    for path in signals_files:
        p = Path(path)
        if not p.exists():
            continue
        try:
            # Read last 2MB to avoid loading huge files
            size = p.stat().st_size
            with open(p, "rb") as f:
                f.seek(max(0, size - 2_000_000))
                chunk = f.read().decode("utf-8", errors="ignore")
            for line in chunk.splitlines():
                try:
                    d = json.loads(line)
                    cid = d.get("condition_id", "")
                    direction = d.get("direction", "")
                    if cid and direction and d.get("executed"):
                        key = (cid, direction)
                        index[key] = d
                except Exception:
                    pass
        except Exception:
            pass
    return index


def extract_live_rows(positions_files, signals_index):
    """Extract settled trade feature rows from positions.jsonl."""
    rows = []
    seen = set()

    for path in positions_files:
        p = Path(path)
        if not p.exists():
            continue
        try:
            with open(p) as f:
                for line in f:
                    try:
                        pos = json.loads(line)
                    except Exception:
                        continue

                    # Only settled trades with known outcome
                    if pos.get("event") != "close":
                        continue
                    status = pos.get("status", "")
                    if status not in ("won", "lost"):
                        continue

                    cid       = pos.get("condition_id", "")
                    direction = pos.get("direction", "")
                    key       = (cid, direction)
                    if key in seen:
                        continue
                    seen.add(key)

                    outcome = 1 if status == "won" else 0
                    end_ts  = float(pos.get("end_time", pos.get("ts_close", time.time())))

                    # Get features from signals index
                    sig = signals_index.get(key, {})
                    if not sig:
                        continue

                    row = {
                        "condition_id":    cid,
                        "symbol":          pos.get("symbol", sig.get("symbol", "")),
                        "direction":       direction,
                        "duration_secs":   int(pos.get("duration_secs", sig.get("duration_secs", 300))),
                        "hour_utc":        sig.get("hour_utc", 0),
                        "minute_utc":      sig.get("minute_utc", 0),
                        "day_of_week":     sig.get("day_of_week", 0),
                        "ask_price":       float(pos.get("entry_price", sig.get("ask_price", 0.5))),
                        "outcome":         outcome,
                        "end_ts":          end_ts,
                        # Signal features — use whatever was logged
                        "s1_momentum":     sig.get("s1_momentum", 0.0),
                        "s1_conf":         sig.get("s1_conf", 0.0),
                        "s3_funding":      sig.get("s3_funding", 0.0),
                        "s3_conf":         sig.get("s3_conf", 0.0),
                        "s9_premium":      sig.get("s9_premium", 0.0),
                        "s10_basis":       sig.get("s10_basis", 0.0),
                        "bn_momentum_1m":  sig.get("bn_momentum_1m", 0.0),
                        "bn_momentum_5m":  sig.get("bn_momentum_5m", 0.0),
                        "bn_momentum_15m": sig.get("bn_momentum_15m", 0.0),
                        "bn_momentum_30m": sig.get("bn_momentum_30m", 0.0),
                        "bn_momentum_60m": sig.get("bn_momentum_60m", 0.0),
                        "bn_volume_ratio": sig.get("bn_volume_ratio", 1.0),
                        "bn_cvd_1m":       sig.get("bn_cvd_1m", 0.0),
                        "realized_vol_30m":sig.get("realized_vol_30m", 0.02),
                        "price_vs_ma20":   sig.get("price_vs_ma20", 0.0),
                        "price_vs_ma60":   sig.get("price_vs_ma60", 0.0),
                        "drift_pct":       sig.get("drift_pct", 0.0),
                        "drift_signed":    sig.get("drift_signed", 0.0),
                        "ev":              sig.get("ev", 0.0),
                        "p_win":           sig.get("p_win", 0.5),
                    }
                    rows.append(row)
        except Exception as e:
            print(f"  Error reading {path}: {e}")

    return rows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] ml_calibrate starting...")

    Path(MODEL_DIR).mkdir(parents=True, exist_ok=True)

    # 1. Extract live training rows
    print("Loading signals index...")
    sig_index = load_signals_index(SIGNALS_FILES)
    print(f"  {len(sig_index):,} executed signals indexed")

    print("Extracting live training rows...")
    live_rows = extract_live_rows(POSITIONS_FILES, sig_index)
    print(f"  {len(live_rows):,} live settled trades with features")

    if len(live_rows) < MIN_LIVE_ROWS:
        print(f"  Not enough live data ({len(live_rows)} < {MIN_LIVE_ROWS}). Skipping retrain.")
        return

    # 2. Write live data
    with open(LIVE_DATA, "w") as f:
        for row in live_rows:
            f.write(json.dumps(row) + "\n")

    # 3. Combine historical + live into one file (both will be filtered by rolling_days)
    print(f"Combining historical + live data (rolling {ROLLING_DAYS} days)...")
    n_combined = 0
    with open(COMBINED_DATA, "w") as out:
        # Historical first
        if Path(HISTORICAL_DATA).exists():
            with open(HISTORICAL_DATA) as f:
                for line in f:
                    out.write(line)
                    n_combined += 1
        # Live on top
        with open(LIVE_DATA) as f:
            for line in f:
                out.write(line)
                n_combined += 1
    print(f"  {n_combined:,} total rows before rolling filter")

    # 4. Retrain with rolling window
    print(f"Retraining model (rolling {ROLLING_DAYS} days)...")
    result = subprocess.run(
        [sys.executable, ML_TRAIN_SCRIPT,
         "--data",         COMBINED_DATA,
         "--out-dir",      MODEL_DIR,
         "--rolling-days", str(ROLLING_DAYS)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"  Training FAILED:\n{result.stderr[-2000:]}")
        return

    # Print key lines from training output
    for line in result.stdout.splitlines():
        if any(kw in line for kw in ["AUC", "Brier", "threshold", "Loaded", "saved"]):
            print(f"  {line.strip()}")

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.0f}s. Model hot-reloaded by bots within 30 min.")


if __name__ == "__main__":
    main()
