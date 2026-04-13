"""
direction_consensus.py — D-bot signal aggregation engine

Queries S1-S7 for a given asset and aggregates votes into a consensus direction.

Usage:
    dc = DirectionConsensus()
    result = await dc.evaluate(client, asset="BTC", token_id="0xabc...", ts_ms=None)

    result.direction      # "UP" | "DOWN" | None (no majority)
    result.conf           # average confidence of agreeing signals
    result.n_signals      # number of agreeing signals (0..7)
    result.signal_details # {"s1": {"direction": "UP", "conf": 0.62}, ...}

Notes:
    - S1-S6 run in parallel via asyncio.gather.
    - S7 is sync (local file I/O, ~1ms); called after S1-S6 to query the
      plurality direction from those signals.
    - token_id="" → S6 returns BALANCED (safe for a first-pass scan).
    - Ties (equal UP/DOWN count) → direction=None, n_signals=0.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from direction_signals import (
    s1_price_action,
    s2_order_flow,
    s3_market_structure,
    s4_liquidations,
    s5_cross_asset,
    s6_poly_flow,
    s7_whale_consensus,
    s11_cvd,
)

logger = logging.getLogger("direction_consensus")

# States that do not count as a directional vote
_NON_SIGNAL = {"FLAT", "NEUTRAL", "NONE", "BALANCED"}


@dataclass
class ConsensusResult:
    direction: Optional[str]   # "UP" | "DOWN" | None
    conf: float                # avg conf of agreeing signals (0 if no direction)
    n_signals: int             # number of signals agreeing with direction
    signal_details: dict = field(default_factory=dict)
    # e.g. {"s1": {"direction": "UP", "conf": 0.62}, "s2": {"direction": "NEUTRAL", "conf": 0.57}, ...}


class DirectionConsensus:
    """Aggregate S1-S7 into a consensus direction + confidence."""

    async def evaluate(
        self,
        client,
        asset: str,
        token_id: str,
        ts_ms: int = None,
    ) -> ConsensusResult:
        """
        Run all 7 signals for asset/token and return voting consensus.

        asset:    "BTC" | "ETH" | "SOL"
        token_id: Polymarket token ID for S6 (pass "" to skip S6)
        ts_ms:    epoch ms (None = now / live mode)
        """
        # S1-S6 + S11 run concurrently
        s1_r, s2_r, s3_r, s4_r, s5_r, s6_r, s11_r = await asyncio.gather(
            s1_price_action(client, asset, ts_ms),
            s2_order_flow(client, asset, ts_ms),
            s3_market_structure(client, asset, ts_ms),
            s4_liquidations(client, asset, ts_ms),
            s5_cross_asset(client, asset, ts_ms),
            s6_poly_flow(client, token_id, ts_ms),
            s11_cvd(client, asset, ts_ms),
        )

        partial = {
            "s1": s1_r, "s2": s2_r, "s3": s3_r,
            "s4": s4_r, "s5": s5_r, "s6": s6_r, "s11": s11_r,
        }

        # Determine plurality direction from S1-S6+S11 to query S7
        up_partial   = sum(1 for d, _ in partial.values() if d == "UP")
        down_partial = sum(1 for d, _ in partial.values() if d == "DOWN")

        if up_partial > down_partial:
            s7_query_dir = "Up"
        elif down_partial > up_partial:
            s7_query_dir = "Down"
        else:
            s7_query_dir = None  # tie or no signals — S7 won't help resolve

        if s7_query_dir:
            s7_r = s7_whale_consensus(asset, s7_query_dir, ts_ms)
        else:
            s7_r = ("NONE", 0.57)

        all_signals = {**partial, "s7": s7_r}

        # Tally votes
        up_votes   = {k: c for k, (d, c) in all_signals.items() if d == "UP"}
        down_votes = {k: c for k, (d, c) in all_signals.items() if d == "DOWN"}

        n_up   = len(up_votes)
        n_down = len(down_votes)

        if n_up > n_down:
            direction = "UP"
            agreeing  = up_votes
        elif n_down > n_up:
            direction = "DOWN"
            agreeing  = down_votes
        else:
            # Tie or all neutral — no consensus
            return ConsensusResult(
                direction=None,
                conf=0.0,
                n_signals=0,
                signal_details={
                    k: {"direction": d, "conf": c}
                    for k, (d, c) in all_signals.items()
                },
            )

        avg_conf = sum(agreeing.values()) / len(agreeing)

        return ConsensusResult(
            direction=direction,
            conf=round(avg_conf, 3),
            n_signals=max(n_up, n_down),
            signal_details={
                k: {"direction": d, "conf": c}
                for k, (d, c) in all_signals.items()
            },
        )
