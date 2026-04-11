"""
ml_train.py — XGBoost win-rate model trainer.

Reads training_data.jsonl (built by ml_backfill.py or accumulated live),
trains a calibrated XGBoost classifier, saves model + scaler.

Output:
  /root/shared_ml/signal_model.json     — XGBoost model
  /root/shared_ml/model_meta.json       — feature list, threshold, training stats

Usage:
  python ml_train.py [--data training_data.jsonl] [--out-dir /root/shared_ml]
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

FEATURE_COLS = [
    "ask_price",
    "hour_utc",
    "minute_utc",
    "day_of_week",
    "duration_secs",
    "s1_momentum",
    "s1_conf",
    "s3_funding",
    "s3_conf",
    # s9_premium, s10_basis, bn_cvd_1m, bn_oi_change dropped — 90-100% zeros in
    # historical data, adding noise. Will re-add once live data accumulates.
    "bn_momentum_1m",
    "bn_momentum_5m",
    "bn_momentum_15m",
    "bn_momentum_30m",
    "bn_momentum_60m",
    "bn_volume_ratio",
    "realized_vol_30m",
    "price_vs_ma20",
    "price_vs_ma60",
    "drift_pct",
    "drift_signed",
    "ev",
    "p_win",
    # One-hot encoded at training time:
    # symbol_BTC, symbol_ETH, symbol_DOGE, symbol_XRP
    # direction_Up, direction_Down
    # Symbol×direction interactions (explicit combos with different base WRs):
    # combo_BTC_Up, combo_BTC_Down, combo_ETH_Up, combo_ETH_Down,
    # combo_DOGE_Up, combo_DOGE_Down, combo_XRP_Up, combo_XRP_Down
]

SYMBOL_COLS    = ["symbol_BTC", "symbol_ETH", "symbol_DOGE", "symbol_XRP"]
DIRECTION_COLS = ["direction_Up", "direction_Down"]
COMBO_COLS     = [f"combo_{s}_{d}" for s in ["BTC", "ETH", "DOGE", "XRP"]
                                   for d in ["Up", "Down"]]
ENGINEERED_COLS = [
    "mom_agreement",    # CB × BN momentum product
    "mom_consistency",  # 5-min trend consistent with 1-min?
    "is_peak_hour",     # 09-10, 14, 18-21 UTC
    "is_dead_hour",     # 11-12 UTC
    "is_strict_hour",   # 06-08, 13, 15-17 UTC
    "ev_x_duration",    # EV weighted by market shortness
    "cheap_entry",      # ask_price < 0.45
]
ALL_FEATURE_COLS = FEATURE_COLS + SYMBOL_COLS + DIRECTION_COLS + COMBO_COLS + ENGINEERED_COLS


def load_training_data(path: str, rolling_days: int = None):
    """Load training_data.jsonl → list of dicts.

    If rolling_days is set, only keep rows whose end_ts falls within the
    last N days. This keeps the model calibrated to recent market structure
    rather than diluting with stale regime data.
    """
    import time as _time
    cutoff = (_time.time() - rolling_days * 86400) if rolling_days else 0

    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("outcome") is None:
                    continue
                if cutoff and d.get("end_ts", 0) < cutoff:
                    continue
                rows.append(d)
            except Exception:
                pass
    return rows


def prepare_xy(rows: list):
    """Convert rows to feature matrix X and label vector y."""
    import pandas as pd
    import numpy as np

    df = pd.DataFrame(rows)
    print(f"  Loaded {len(df)} rows, {df['outcome'].mean():.3f} win rate")

    # One-hot encode symbol and direction
    for sym in ["BTC", "ETH", "DOGE", "XRP"]:
        df[f"symbol_{sym}"] = (df["symbol"] == sym).astype(float)
    for d in ["Up", "Down"]:
        df[f"direction_{d}"] = (df["direction"] == d).astype(float)

    # Symbol×direction interaction features — explicit combos with different base WRs
    # e.g. DOGE Up ~70% WR vs ETH Down ~38% WR; combined feature makes this obvious
    for sym in ["BTC", "ETH", "DOGE", "XRP"]:
        for d in ["Up", "Down"]:
            df[f"combo_{sym}_{d}"] = (
                (df["symbol"] == sym) & (df["direction"] == d)
            ).astype(float)

    # Engineered features from existing columns — no backfill rerun needed

    # Momentum agreement: CB and BN pointing same direction = stronger signal
    df["mom_agreement"] = (df["bn_momentum_1m"] * df["s1_momentum"]).clip(-1e-4, 1e-4) * 1e4

    # Momentum consistency: is 5-min trend consistent with 1-min?
    # Positive = accelerating in same direction, negative = fading/reversing
    eps = 1e-9
    df["mom_consistency"] = (
        df["bn_momentum_5m"] * df["bn_momentum_1m"].abs() /
        (df["bn_momentum_1m"].abs() + eps)
    ).clip(-0.01, 0.01) * 100

    # Hour-of-day buckets (D2 bot zones)
    df["is_peak_hour"]   = df["hour_utc"].isin([9,10,14,18,19,20,21]).astype(float)
    df["is_dead_hour"]   = df["hour_utc"].isin([11,12]).astype(float)
    df["is_strict_hour"] = df["hour_utc"].isin([6,7,8,13,15,16,17]).astype(float)

    # EV × duration: EV is more exploitable in short markets (less time for mean reversion)
    df["ev_x_duration"] = df["ev"] * (1.0 / (df["duration_secs"].clip(60, 3600) / 300.0))

    # Cheap market flag: entering below 0.45 = higher payout if right
    df["cheap_entry"] = (df["ask_price"] < 0.45).astype(float)

    # Fill missing features with 0
    for col in ALL_FEATURE_COLS:
        if col not in df.columns:
            df[col] = 0.0

    X = df[ALL_FEATURE_COLS].fillna(0.0).values
    y = df["outcome"].astype(int).values
    return X, y, df


def train(data_path: str, out_dir: str, min_rows: int = 200, rolling_days: int = None):
    try:
        import xgboost as xgb
        from sklearn.calibration import CalibratedClassifierCV
        from sklearn.model_selection import StratifiedKFold, cross_val_score
        import numpy as np
    except ImportError:
        print("pip install xgboost scikit-learn numpy pandas")
        sys.exit(1)

    print(f"Loading {data_path}..." + (f" (last {rolling_days} days)" if rolling_days else ""))
    rows = load_training_data(data_path, rolling_days)
    if len(rows) < min_rows:
        print(f"Not enough data ({len(rows)} rows, need {min_rows}). Skipping.")
        return None

    print("Preparing features...")
    X, y, df = prepare_xy(rows)

    # ── Cross-validated AUC ───────────────────────────────────────────────────
    base_model = xgb.XGBClassifier(
        n_estimators=600,       # more trees (was 300)
        max_depth=6,            # deeper trees (was 4) — more feature interactions
        learning_rate=0.03,     # lower LR with more trees
        subsample=0.8,
        colsample_bytree=0.7,   # slightly more aggressive column sampling
        min_child_weight=15,    # prevent overfitting (raised from 10)
        gamma=1.0,              # min loss reduction to split — regularization
        scale_pos_weight=1.0,
        eval_metric="logloss",
        random_state=42,
        n_jobs=-1,
    )

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    auc_scores = cross_val_score(base_model, X, y, cv=cv, scoring="roc_auc")
    print(f"  CV AUC: {auc_scores.mean():.4f} ± {auc_scores.std():.4f}")

    brier_scores = cross_val_score(base_model, X, y, cv=cv, scoring="neg_brier_score")
    print(f"  CV Brier: {-brier_scores.mean():.4f} ± {brier_scores.std():.4f}")

    # ── Train final model with Platt scaling calibration ─────────────────────
    print("Training calibrated model...")
    calibrated = CalibratedClassifierCV(base_model, method="sigmoid", cv=5)
    calibrated.fit(X, y)

    # ── Feature importance ────────────────────────────────────────────────────
    # Get importance from the first estimator in the calibration ensemble
    try:
        importances = calibrated.calibrated_classifiers_[0].estimator.feature_importances_
        feat_imp = sorted(zip(ALL_FEATURE_COLS, importances),
                          key=lambda x: x[1], reverse=True)
        print("\nTop 10 features:")
        for name, imp in feat_imp[:10]:
            print(f"  {name:25s} {imp:.4f}")
    except Exception:
        feat_imp = []

    # ── Find optimal threshold (maximize edge vs breakeven) ───────────────────
    probs = calibrated.predict_proba(X)[:, 1]
    # Threshold: p_win * 0.99 - avg_ask > EV_THRESHOLD
    # Simplified: find p_win threshold where precision >= 0.57 (breakeven + buffer)
    thresholds = [0.50, 0.52, 0.54, 0.55, 0.56, 0.57, 0.58, 0.60, 0.62, 0.65]
    print("\nThreshold analysis:")
    best_threshold = 0.55
    for thr in thresholds:
        mask = probs >= thr
        if mask.sum() < 20:
            break
        prec = y[mask].mean()
        n    = mask.sum()
        print(f"  p>={thr:.2f}: n={n:4d}  WR={prec:.3f}")
        if prec >= 0.575 and n >= 50:
            best_threshold = thr

    print(f"\nSelected threshold: {best_threshold}")

    # ── Save model ────────────────────────────────────────────────────────────
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    import pickle
    model_path = out_path / "signal_model.pkl"
    with open(model_path, "wb") as f:
        pickle.dump(calibrated, f)
    print(f"\nModel saved: {model_path}")

    meta = {
        "trained_at":    time.time(),
        "n_samples":     int(len(rows)),
        "win_rate":      float(y.mean()),
        "cv_auc":        float(auc_scores.mean()),
        "cv_auc_std":    float(auc_scores.std()),
        "cv_brier":      float(-brier_scores.mean()),
        "threshold":     float(best_threshold),
        "feature_cols":  ALL_FEATURE_COLS,
        "top_features":  [(n, float(i)) for n, i in feat_imp[:15]],
        "symbol_counts": df["symbol"].value_counts().to_dict(),
        "direction_counts": df["direction"].value_counts().to_dict(),
    }
    meta_path = out_path / "model_meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"Meta saved:  {meta_path}")

    return calibrated, meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data",         default="C:/tmp/ml_data/training_data.jsonl")
    parser.add_argument("--out-dir",      default="C:/tmp/ml_data/model")
    parser.add_argument("--min-rows",     type=int, default=200)
    parser.add_argument("--rolling-days", type=int, default=None,
                        help="Only train on last N days of data (e.g. 90)")
    args = parser.parse_args()

    train(args.data, args.out_dir, args.min_rows, args.rolling_days)
