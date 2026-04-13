"""
threshold_engine.py — Dynamic threshold computation from S1-S6 directional signals

For each incoming whale signal the Threshold Engine queries S1-S6 and adjusts
the base CCC threshold based on agreement / disagreement with the whale direction.

    base_threshold = THRESHOLD_BASE  (default 0.60)

    for each S in [S1..S6]:
        agrees with whale direction → lower threshold by THRESHOLD_STEP
        neutral / flat / balanced   → no change
        disagrees                   → raise threshold by THRESHOLD_STEP

    threshold = clamp(base + adjustment, THRESHOLD_MIN, THRESHOLD_MAX)

S7 is NOT evaluated here — S7 reads C-bot/Whalebot signals, creating a
circular dependency.

Signals use TTL caching inside direction_signals.py — calls are cheap when
the same asset is evaluated multiple times within one 5-min bucket.
"""
import asyncio
import logging
import os

from direction_signals import (
    s1_price_action,
    s2_order_flow,
    s3_market_structure,
    s4_liquidations,
    s5_cross_asset,
    s6_poly_flow,
)

logger = logging.getLogger("threshold_engine")

THRESHOLD_BASE = float(os.getenv("THRESHOLD_BASE", "0.60"))
THRESHOLD_MIN  = float(os.getenv("THRESHOLD_MIN",  "0.45"))
THRESHOLD_MAX  = float(os.getenv("THRESHOLD_MAX",  "0.82"))
THRESHOLD_STEP = float(os.getenv("THRESHOLD_STEP", "0.04"))

_NON_SIGNAL = {"FLAT", "NEUTRAL", "NONE", "BALANCED"}


class ThresholdEngine:
    """
    Computes a per-evaluation dynamic threshold from S1-S6 signals.

    Each call fires S1-S6 concurrently (TTL-cached in direction_signals).
    Failures in individual signals are tolerated — the signal is treated
    as neutral and contributes zero adjustment.
    """

    async def compute(
        self,
        client,
        asset:      str,
        direction:  str,   # "Up" or "Down" — Polymarket convention
        token_id:   str,   # for S6 (pass "" if not yet known)
        ts_ms:      int = None,
    ) -> tuple[float, dict]:
        """
        Returns (threshold, signal_adjustments).

        signal_adjustments: {"s1": -0.04, "s2": 0.0, ...}
        """
        whale_dir = direction.upper()   # S1-S6 use "UP" / "DOWN"

        try:
            s1_r, s2_r, s3_r, s4_r, s5_r, s6_r = await asyncio.gather(
                s1_price_action(client,   asset,    ts_ms),
                s2_order_flow(client,     asset,    ts_ms),
                s3_market_structure(client, asset,  ts_ms),
                s4_liquidations(client,   asset,    ts_ms),
                s5_cross_asset(client,    asset,    ts_ms),
                s6_poly_flow(client,      token_id, ts_ms),
                return_exceptions=True,
            )
        except Exception as e:
            logger.warning(f"ThresholdEngine: gather failed for {asset}: {e}")
            return THRESHOLD_BASE, {}

        raw = {
            "s1": s1_r, "s2": s2_r, "s3": s3_r,
            "s4": s4_r, "s5": s5_r, "s6": s6_r,
        }

        total_adj   = 0.0
        adjustments = {}

        for name, result in raw.items():
            if isinstance(result, Exception):
                logger.debug(f"ThresholdEngine: {name} raised {result}")
                adjustments[name] = 0.0
                continue

            sig_dir, _sig_conf = result
            if sig_dir in _NON_SIGNAL:
                adj = 0.0
            elif sig_dir == whale_dir:
                adj = -THRESHOLD_STEP    # supporting signal → lower the bar
            else:
                adj = +THRESHOLD_STEP    # opposing signal  → raise the bar

            adjustments[name] = round(adj, 4)
            total_adj += adj

        threshold = THRESHOLD_BASE + total_adj
        threshold = max(THRESHOLD_MIN, min(THRESHOLD_MAX, threshold))
        threshold = round(threshold, 4)

        logger.debug(
            f"ThresholdEngine: {asset} {direction} | "
            f"base={THRESHOLD_BASE} adj={total_adj:+.2f} → thr={threshold} | "
            f"sigs={[(k, raw[k][0] if not isinstance(raw[k], Exception) else 'ERR') for k in raw]}"
        )

        return threshold, adjustments
