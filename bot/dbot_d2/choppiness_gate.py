"""
choppiness_gate.py — Global market choppiness gate.

Polls 1-min BTC-USD candles from Coinbase every EVAL_INTERVAL seconds.
Sets a global PAUSED flag when mean intrabar range exceeds PAUSE_THRESHOLD.
Uses hysteresis: only resumes when chop falls below RESUME_THRESHOLD.

Usage:
    gate = ChoppinessGate()
    asyncio.create_task(gate.update_loop())   # start background loop

    if gate.is_paused():
        return   # skip trade
"""
import asyncio
import logging
import os
import time
from statistics import mean
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

PAUSE_THRESHOLD  = float(os.getenv("CHOP_PAUSE_THRESHOLD",  "0.008"))
RESUME_THRESHOLD = float(os.getenv("CHOP_RESUME_THRESHOLD", "0.005"))
EVAL_INTERVAL    = int(os.getenv("CHOP_EVAL_INTERVAL",      "60"))

_CANDLES_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


async def _fetch_chop(client: httpx.AsyncClient) -> Optional[float]:
    """
    Fetch last 5 one-minute BTC-USD candles from Coinbase.
    Returns mean intrabar range as fraction of midprice, or None on failure.
    Response format: [[ts, low, high, open, close, volume], ...]
    """
    try:
        resp = await client.get(
            _CANDLES_URL,
            params={"granularity": 60, "limit": 5},
            timeout=10.0,
        )
        resp.raise_for_status()
        candles = resp.json()
        if not candles:
            return None
        ranges = []
        for c in candles[:5]:
            low  = float(c[1])
            high = float(c[2])
            mid  = (low + high) / 2.0
            if mid > 0:
                ranges.append((high - low) / mid)
        return mean(ranges) if ranges else None
    except Exception as e:
        logger.warning(f"ChoppinessGate: candle fetch failed: {e}")
        return None


class ChoppinessGate:
    def __init__(self):
        if RESUME_THRESHOLD >= PAUSE_THRESHOLD:
            raise ValueError(
                f"CHOP_RESUME_THRESHOLD ({RESUME_THRESHOLD}) must be < "
                f"CHOP_PAUSE_THRESHOLD ({PAUSE_THRESHOLD})"
            )
        self._paused:    bool            = False
        self._last_chop: Optional[float] = None
        self._last_eval: float           = 0.0

    def is_paused(self) -> bool:
        """Return True if market is currently too choppy to trade."""
        return self._paused

    async def _update_once(self, client: Optional[httpx.AsyncClient] = None):
        """Single evaluation step. Exposed for testing."""
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient()
        try:
            chop = await _fetch_chop(client)
        finally:
            if own_client:
                await client.aclose()

        if chop is None:
            return   # retain current state

        old_state = self._paused
        if chop > PAUSE_THRESHOLD:
            self._paused = True
        elif chop < RESUME_THRESHOLD:
            self._paused = False
        # between thresholds: retain current state (hysteresis)

        self._last_chop = chop
        self._last_eval = time.time()

        if self._paused != old_state:
            state_str = "PAUSED" if self._paused else "ACTIVE"
            logger.info(
                f"ChoppinessGate: -> {state_str}  "
                f"chop={chop:.5f}  "
                f"(pause>{PAUSE_THRESHOLD}  resume<{RESUME_THRESHOLD})"
            )
        else:
            logger.debug(
                f"ChoppinessGate: {'PAUSED' if self._paused else 'active'}  "
                f"chop={chop:.5f}"
            )

    async def update_loop(self):
        """Background loop — run as asyncio task. Polls every EVAL_INTERVAL seconds."""
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await self._update_once(client)
                except Exception as e:
                    logger.error(f"ChoppinessGate: update_loop error: {e}", exc_info=True)
                await asyncio.sleep(EVAL_INTERVAL)
