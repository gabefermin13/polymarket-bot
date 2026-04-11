"""
coinbase_ws.py — Real-time Coinbase price feed via persistent WebSocket.

Maintains a reconnecting asyncio WebSocket connection to Coinbase Exchange,
subscribed to the ticker channel for all tracked assets.

Eliminates the 6-10s REST polling latency for current price lookups.
Falls back gracefully if the connection hasn't received a price yet (returns
None → callers fall through to REST).

Usage:
    ws = CoinbaseWS(assets=["BTC", "ETH", "DOGE", "XRP"])
    await ws.start()              # call once at startup
    price = ws.get_price("BTC")  # instant, no I/O
    await ws.stop()               # clean shutdown

Module-level singleton (used by drift_ev.py):
    from coinbase_ws import set_instance, get_instance
"""
import asyncio
import json
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger("coinbase_ws")

_WS_URL          = "wss://ws-feed.exchange.coinbase.com"
_RECONNECT_DELAY = 5    # seconds between reconnect attempts
_STALE_SECS      = 30   # price older than this treated as None

# Module-level singleton — set by main(), read by drift_ev.py
_instance: Optional["CoinbaseWS"] = None

def get_instance() -> Optional["CoinbaseWS"]:
    return _instance

def set_instance(ws: "CoinbaseWS"):
    global _instance
    _instance = ws


class CoinbaseWS:
    def __init__(self, assets: list = None):
        self._assets   = assets or ["BTC", "ETH", "DOGE", "XRP"]
        self._products = [f"{a}-USD" for a in self._assets]
        self._prices:  Dict[str, float] = {}
        self._ts:      Dict[str, float] = {}
        self._task:    Optional[asyncio.Task] = None
        self._running  = False

    def get_price(self, asset: str) -> Optional[float]:
        """Return latest WebSocket price, or None if stale/not yet received."""
        ts = self._ts.get(asset, 0)
        if time.time() - ts > _STALE_SECS:
            return None
        return self._prices.get(asset)

    def age(self, asset: str) -> float:
        """Seconds since last price update for this asset."""
        return time.time() - self._ts.get(asset, 0)

    async def start(self):
        """Start WebSocket listener in the background."""
        if self._running:
            return
        self._running = True
        self._task    = asyncio.create_task(self._run_loop())
        logger.info(f"CoinbaseWS started — {self._products}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self):
        """Reconnect loop — keeps connection alive indefinitely."""
        try:
            import websockets
        except ImportError:
            logger.error("CoinbaseWS requires 'websockets' package: pip install websockets")
            return

        while self._running:
            try:
                async with websockets.connect(
                    _WS_URL,
                    ping_interval = 20,
                    ping_timeout  = 10,
                ) as ws:
                    await ws.send(json.dumps({
                        "type":        "subscribe",
                        "product_ids": self._products,
                        "channels":    ["ticker"],
                    }))
                    logger.info("CoinbaseWS connected and subscribed")

                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") == "ticker":
                                product = msg.get("product_id", "")
                                price   = msg.get("price")
                                if product and price:
                                    asset = product.replace("-USD", "")
                                    self._prices[asset] = float(price)
                                    self._ts[asset]     = time.time()
                        except Exception:
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    logger.warning(
                        f"CoinbaseWS disconnected: {e} — reconnecting in {_RECONNECT_DELAY}s"
                    )
                    await asyncio.sleep(_RECONNECT_DELAY)

        logger.info("CoinbaseWS stopped")
