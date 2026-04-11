"""
ml_predict.py — Live ML inference for D2 and W-bot.

Loads the trained XGBoost model and returns a calibrated P(win) score
for any candidate trade at entry time. Replaces the sequential gate
stack with a single score — trades below threshold are sized down or
skipped, not hard-blocked.

Designed to be imported by d_main.py and w_main.py.

Usage in d_main.py:
    from ml_predict import MLPredictor
    _ml = MLPredictor("/root/shared_ml")
    ...
    score = await _ml.score(symbol, direction, start_ts, end_ts, ask_price)
    if score.p_win < ML_HARD_FLOOR:
        continue  # skip
    kelly_multiplier *= score.kelly_scale  # size by confidence

Hot-reload: model is reloaded from disk if model_meta.json is newer
than when it was last loaded (ccc_reload_loop calls _ml.maybe_reload()).
"""
import asyncio
import json
import math
import os
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ── Score result ──────────────────────────────────────────────────────────────
@dataclass
class MLScore:
    p_win:        float   # calibrated P(win) from model
    ev:           float   # p_win * 0.99 - ask_price
    kelly_scale:  float   # how much to multiply Kelly by (0.5 – 1.5)
    features:     dict    # raw feature dict (for logging)
    model_ver:    float   # training timestamp of model used
    has_model:    bool    # False = model not loaded, use fallback


# ── Predictor ─────────────────────────────────────────────────────────────────
class MLPredictor:
    """
    Thread-safe (asyncio) ML predictor with hot-reload.

    Args:
        model_dir: directory containing signal_model.pkl + model_meta.json
        hard_floor: skip trades with p_win below this (default 0.52)
        fallback_threshold: p_win to assume when model not loaded
    """

    def __init__(self,
                 model_dir: str = "/root/shared_ml",
                 hard_floor: float = 0.52,
                 fallback_threshold: float = 0.55):
        self._model_dir   = Path(model_dir)
        self._hard_floor  = float(os.getenv("ML_HARD_FLOOR", hard_floor))
        self._fallback    = fallback_threshold
        self._model       = None     # CalibratedClassifierCV
        self._meta        = {}
        self._loaded_at   = 0.0
        self._meta_mtime  = 0.0
        self._lock        = asyncio.Lock()

        # Try loading immediately
        self._try_load()

    # ── Model loading ─────────────────────────────────────────────────────────
    def _try_load(self) -> bool:
        pkl_path  = self._model_dir / "signal_model.pkl"
        meta_path = self._model_dir / "model_meta.json"
        if not pkl_path.exists() or not meta_path.exists():
            return False
        try:
            mtime = meta_path.stat().st_mtime
            if mtime <= self._meta_mtime:
                return False   # already up-to-date
            with open(pkl_path, "rb") as f:
                model = pickle.load(f)
            with open(meta_path) as f:
                meta = json.load(f)
            self._model      = model
            self._meta       = meta
            self._loaded_at  = time.time()
            self._meta_mtime = mtime
            n   = meta.get("n_samples", "?")
            auc = meta.get("cv_auc", 0)
            thr = meta.get("threshold", self._hard_floor)
            print(f"[ML] Model loaded: n={n} AUC={auc:.4f} threshold={thr:.3f} "
                  f"trained={time.strftime('%H:%M UTC', time.gmtime(meta.get('trained_at', 0)))}")
            return True
        except Exception as e:
            print(f"[ML] Load failed: {e}")
            return False

    def maybe_reload(self):
        """Call from ccc_reload_loop every 30 min. Non-async, safe to call from thread."""
        self._try_load()

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def threshold(self) -> float:
        return float(self._meta.get("threshold", self._hard_floor))

    # ── Feature vector construction ───────────────────────────────────────────
    def _make_X(self, features: dict):
        """Convert feature dict to numpy array matching training column order."""
        import numpy as np

        feature_cols = self._meta.get("feature_cols", [])
        if not feature_cols:
            # Fallback column order (matches ml_train.py ALL_FEATURE_COLS)
            feature_cols = [
                "ask_price", "hour_utc", "minute_utc", "day_of_week",
                "duration_secs", "s1_momentum", "s1_conf", "s3_funding",
                "s3_conf", "s9_premium", "s10_basis", "bn_momentum_1m",
                "bn_momentum_5m", "bn_volume_ratio", "bn_cvd_1m",
                "bn_oi_change", "drift_pct", "drift_signed", "ev", "p_win",
                "symbol_BTC", "symbol_ETH", "symbol_DOGE", "symbol_XRP",
                "direction_Up", "direction_Down",
            ]

        row = [float(features.get(col, 0.0)) for col in feature_cols]
        return np.array(row, dtype=float).reshape(1, -1)

    # ── Main scoring method ───────────────────────────────────────────────────
    async def score(self,
                    symbol: str,
                    direction: str,
                    start_ts: float,
                    end_ts: float,
                    ask_price: float,
                    extra_features: dict = None) -> MLScore:
        """
        Build features and return ML score for a candidate trade.
        extra_features: pre-computed values (e.g. drift, ev from drift_ev.py)
                        to avoid duplicate API calls.
        """
        from ml_features import (
            _fetch_s1_coinbase, _fetch_s3_funding,
            _fetch_bn_momentum, _fetch_bn_volume_ratio,
            _fetch_bn_cvd, _fetch_bn_oi_change,
            _fetch_s9_premium, _fetch_s10_basis,
            DIRECTION_SIGN,
        )
        import httpx
        import datetime as dt

        dsign = DIRECTION_SIGN.get(direction, 1)
        utc   = dt.datetime.utcfromtimestamp(start_ts)

        features = {
            "ask_price":     ask_price,
            "hour_utc":      utc.hour,
            "minute_utc":    utc.minute,
            "day_of_week":   utc.weekday(),
            "duration_secs": int(end_ts - start_ts),
            # one-hot
            "symbol_BTC":    1.0 if symbol == "BTC"  else 0.0,
            "symbol_ETH":    1.0 if symbol == "ETH"  else 0.0,
            "symbol_DOGE":   1.0 if symbol == "DOGE" else 0.0,
            "symbol_XRP":    1.0 if symbol == "XRP"  else 0.0,
            "direction_Up":  1.0 if direction == "Up"   else 0.0,
            "direction_Down":1.0 if direction == "Down" else 0.0,
        }

        # Merge pre-computed features (drift, ev, p_win from drift_ev.py)
        if extra_features:
            features.update(extra_features)

        # Fetch remaining signal features in parallel
        async with httpx.AsyncClient(timeout=8) as client:
            results = await asyncio.gather(
                _fetch_s1_coinbase(client, symbol, start_ts),
                _fetch_s3_funding(client, symbol),
                _fetch_bn_momentum(client, symbol, start_ts),
                _fetch_bn_volume_ratio(client, symbol, start_ts),
                _fetch_bn_cvd(client, symbol, start_ts),
                _fetch_bn_oi_change(client, symbol, start_ts),
                _fetch_s9_premium(client, symbol),
                _fetch_s10_basis(client, symbol),
                return_exceptions=True,
            )

        keys = [
            ("s1_momentum", "s1_conf"),
            ("s3_funding",  "s3_conf"),
            ("bn_momentum_1m", "bn_momentum_5m"),
            ("bn_volume_ratio",),
            ("bn_cvd_1m",),
            ("bn_oi_change",),
            ("s9_premium",),
            ("s10_basis",),
        ]
        # Directional features to sign-flip
        directional = {"s1_momentum", "s3_funding", "bn_momentum_1m",
                       "bn_momentum_5m", "bn_cvd_1m", "s9_premium",
                       "s10_basis", "drift_signed"}

        for result, kgroup in zip(results, keys):
            if isinstance(result, dict):
                for k, v in result.items():
                    features[k] = v * dsign if k in directional else v
            elif isinstance(result, tuple):
                for k, v in zip(kgroup, result):
                    features[k] = v * dsign if k in directional else v

        # ── Model inference ───────────────────────────────────────────────────
        if not self.ready:
            # No model yet — use simple heuristic from existing signals
            ev     = features.get("ev", 0.0)
            p_win  = features.get("p_win", self._fallback)
            return MLScore(
                p_win=p_win, ev=ev,
                kelly_scale=1.0, features=features,
                model_ver=0.0, has_model=False,
            )

        try:
            X     = self._make_X(features)
            proba = self._model.predict_proba(X)[0]
            p_win = float(proba[1])   # P(outcome=1) = P(win)
        except Exception as e:
            print(f"[ML] Inference error: {e}")
            p_win = features.get("p_win", self._fallback)

        ev = round(p_win * 0.99 - ask_price, 4)

        # Kelly scaling: trades near threshold get 0.5×, strong conviction get 1.5×
        thr   = self.threshold
        scale = _kelly_scale(p_win, thr)

        return MLScore(
            p_win=p_win, ev=ev,
            kelly_scale=scale,
            features=features,
            model_ver=self._meta.get("trained_at", 0.0),
            has_model=True,
        )

    # ── Convenience: should we trade? ─────────────────────────────────────────
    def should_trade(self, score: MLScore, ask_price: float) -> tuple[bool, str]:
        """
        Returns (trade: bool, reason: str).
        Uses ML score if model loaded, else passes through (existing gates handle it).
        """
        if not score.has_model:
            return True, "no_model_passthrough"
        if score.p_win < self._hard_floor:
            return False, f"ml_low_pwin_{score.p_win:.3f}"
        if score.ev < float(os.getenv("ML_MIN_EV", "0.02")):
            return False, f"ml_low_ev_{score.ev:.3f}"
        return True, "ml_pass"


# ── Kelly scale helper ────────────────────────────────────────────────────────
def _kelly_scale(p_win: float, threshold: float) -> float:
    """
    Map p_win to a Kelly multiplier.
    At threshold → 0.5×  (half size, uncertain)
    At 0.60      → 1.0×  (normal)
    At 0.70+     → 1.5×  (high conviction)
    Linear interpolation between breakpoints.
    """
    if p_win <= threshold:
        return 0.5
    if p_win >= 0.70:
        return 1.5
    if p_win <= 0.60:
        # threshold → 0.60 maps to 0.5 → 1.0
        t = (p_win - threshold) / (0.60 - threshold)
        return 0.5 + t * 0.5
    else:
        # 0.60 → 0.70 maps to 1.0 → 1.5
        t = (p_win - 0.60) / (0.70 - 0.60)
        return 1.0 + t * 0.5


# ── Singleton for bot use ─────────────────────────────────────────────────────
_predictor: Optional[MLPredictor] = None

def get_predictor(model_dir: str = "/root/shared_ml") -> MLPredictor:
    """Return shared predictor instance (created once per process)."""
    global _predictor
    if _predictor is None:
        _predictor = MLPredictor(model_dir)
    return _predictor


# ── Test ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    async def test():
        pred = MLPredictor("C:/tmp/ml_data/model")
        print(f"Model ready: {pred.ready}")
        print(f"Threshold:   {pred.threshold}")

        score = await pred.score(
            symbol="BTC", direction="Up",
            start_ts=time.time() - 30,
            end_ts=time.time() + 270,
            ask_price=0.52,
        )
        print(f"\nScore:")
        print(f"  p_win={score.p_win:.4f}")
        print(f"  ev={score.ev:.4f}")
        print(f"  kelly_scale={score.kelly_scale:.2f}x")
        print(f"  has_model={score.has_model}")
        print(f"\nFeatures:")
        for k, v in sorted(score.features.items()):
            if isinstance(v, float):
                print(f"  {k:25s} {v:.6f}")
            else:
                print(f"  {k:25s} {v}")

        trade, reason = pred.should_trade(score, ask_price=0.52)
        print(f"\nshould_trade: {trade}  ({reason})")

    asyncio.run(test())
