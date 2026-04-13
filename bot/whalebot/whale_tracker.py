"""
whale_tracker_w.py — Whalebot variant of WhaleTracker

Differences from C-bot whale_tracker.py:
  1. WebSocket: wss://polygon.drpc.org  (publicnode broke log streaming)
  2. Loser suppression: only blocks if loser is on one side AND no whale on the other.
     A loser-vs-whale trade is still copied from the whale's perspective.
  3. WHALE_ADDRESSES / WHALE_CONFIDENCE start with the curated baseline but
     are updated in-place by w_main.py hot-reload loops (no restart needed).

Deploy as `whale_tracker.py` inside /root/kalshiedge_whalebot/.
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

import websockets
import websockets.exceptions

from market_finder import MarketFinder

logger = logging.getLogger(__name__)

POLYGON_WS          = "wss://polygon.drpc.org"
CTF_ADDRESS         = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
ORDER_FILLED_TOPIC  = "0xd0a08e8c493f9c94f29311604c9de1b4e8c8d4c06bd0c789af57f2d65bfec0f6"

# ── Losers — used only to suppress when no whale is on the other side ─────────
LOSER_ADDRESSES: dict[str, str] = {
    "0x8e6f14c69f5ea3f7b63f2e25adc7c26e7f8fdde8": "zhutoupienao",
    "0x3a4e4f3c4a9d2b1e8f7c6d5a4b3c2d1e0f9e8d7c": "sherlockhomie",
}

# ── Unified whale pool — populated/updated by hot-reload in w_main.py ─────────
# This is the merged set of manually curated + scan-managed wallets that pass
# MIN_WHALE_CONF=0.60.  Hardcoded baseline; reload replaces/extends at runtime.
WHALE_ADDRESSES: dict[str, str] = {
    # ── Manually curated (original C1/C2) ─────────────────────────────────────
    # ── Scan-managed (99%+ WR, n >= 35) ──────────────────────────────────────
    "0xede1be4a7e792c6938ebbe474f6b3327bd038ad7": "scan_99wr_211n",
    "0xca1e9be6468d3d0a491b259f014b6b7017c3a35b": "scan_99wr_143n",
    "0x82b3b78e24f56d59f55b8d07a63c38c6655e0c0e": "scan_97wr_35n",
    "0x0f863d92dd2b960e3eb6a23a35fd92a91981404e": "scan_90wr_116n",
    "0x684a6fb97f7577a2bd6fcad385f9912d71d7540a": "scan_100wr_51n",
    "0xe6cc2b9026e82d1b7ad674dedd9d5a9724db533b": "scan_93wr_119n",
    "0x8f60401fbd2233d5d7979c09de04868a5ec4823e": "scan_93wr_96n",
    "0x42451cb444d0f4c94e0eaef3890f729e55b57d9e": "scan_90wr_39n",
    "0xe63160b37ef1902364a75ea40e1475cf20457226": "scan_84wr_139n",
    "0xd0f579e10858649be19185e2fec2723fbe87fd22": "scan_82wr_40n",
    "0x9e38fd2c06b97aa43caa81f20ec83fcc2811f11a": "scan_82wr_56n",
    "0xeb4580aa16358d6afb4351eeb9100de41c535d24": "scan_80wr_162n",
}

# Per-wallet CCC confidence weights — overwritten by ccc_calibrate.py output
WHALE_CONFIDENCE: dict[str, float] = {
    "0x44ab68a9e1272216005366a9a955e5320d28bef3": 0.75,
    "0x2c692786f409df9e44bec5cf50663667aced6ad0": 0.72,
    "0xb02188c268290bb758d6c261bdf4e552c6e1ebd9": 0.67,
    "0xeded79e9c229d9ec3d55953aee98d6135f5e2b99": 0.60,
    "0xede1be4a7e792c6938ebbe474f6b3327bd038ad7": 0.92,
    "0xca1e9be6468d3d0a491b259f014b6b7017c3a35b": 0.91,
    "0x82b3b78e24f56d59f55b8d07a63c38c6655e0c0e": 0.88,
    "0x0f863d92dd2b960e3eb6a23a35fd92a91981404e": 0.85,
    "0x684a6fb97f7577a2bd6fcad385f9912d71d7540a": 0.84,
    "0xe6cc2b9026e82d1b7ad674dedd9d5a9724db533b": 0.83,
    "0x8f60401fbd2233d5d7979c09de04868a5ec4823e": 0.82,
    "0x42451cb444d0f4c94e0eaef3890f729e55b57d9e": 0.81,
    "0xe63160b37ef1902364a75ea40e1475cf20457226": 0.80,
    "0xd0f579e10858649be19185e2fec2723fbe87fd22": 0.78,
    "0x9e38fd2c06b97aa43caa81f20ec83fcc2811f11a": 0.77,
    "0xeb4580aa16358d6afb4351eeb9100de41c535d24": 0.76,
}


@dataclass
class WhaleTrade:
    whale_addr:   str
    whale_name:   str
    condition_id: str
    token_id:     str
    direction:    str    # "Up" or "Down"
    symbol:       str    # "BTC-USD" etc.
    title:        str
    confidence:   float  # current CCC weight from WHALE_CONFIDENCE
    end_time:     float  # epoch
    ts:           float  # detection time
    tx_hash:      str
    price:        float  = 0.0  # whale's entry price (USDC per contract)


class WhaleTracker:
    """
    Subscribes to Polygon CTF Exchange OrderFilled events via drpc.org WebSocket.
    Fires on_whale_trade(signal) for every confirmed whale buy on an Up-or-Down market.
    """

    def __init__(self, market_finder: MarketFinder,
                 on_whale_trade: Optional[Callable] = None):
        self._mf             = market_finder
        self._on_whale_trade = on_whale_trade
        self._ws             = None
        self._running        = False
        self._sub_id: Optional[str] = None
        self.events_received  = 0
        self.whale_hits       = 0
        self.signals_fired    = 0
        self.loser_hits       = 0
        self._seen_tx: set[str] = set()

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        logger.info("WhaleTracker starting (Frankfurt relay)")
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.InvalidStatus as e:
                code = getattr(getattr(e, "response", None), "status_code", "?")
                logger.warning(f"Polygon WS connect failed ({code}). Retry in 20s…")
                await asyncio.sleep(20)
            except Exception as e:
                logger.error(f"Polygon WS error: {e}. Reconnecting in 10s…")
                await asyncio.sleep(10)

    async def _connect_and_stream(self):
        """Frankfurt relay client — low-latency Polygon whale events via dedicated relay."""
        import os
        from collections import deque
        RELAY_URI  = os.getenv("POLYGON_RELAY_URI",  "ws://138.197.181.139:8082")
        AUTH_TOKEN = os.getenv("RELAY_AUTH_TOKEN",   "poly_relay_2026")
        # Bounded dedupe — prevents duplicate trades when Frankfurt reconnects
        # and eth_subscribe replays recent events. 500 entries ~ last few blocks.
        _seen: deque = deque(maxlen=500)
        logger.info(f"WhaleTracker: connecting to Frankfurt relay {RELAY_URI}")
        while True:
            try:
                async with websockets.connect(
                    RELAY_URI,
                    ping_interval=None,
                    close_timeout=10,
                ) as ws:
                    await ws.send(AUTH_TOKEN)
                    logger.info("WhaleTracker: Frankfurt relay connected and authenticated")
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=90)
                        except asyncio.TimeoutError:
                            logger.critical(
                                "WhaleTracker: no relay events for 90s — "
                                "reconnecting to Frankfurt"
                            )
                            break
                        try:
                            event_log = json.loads(raw)
                            # Dedupe guard: Frankfurt reconnect can replay recent events
                            dedup_key = (
                                event_log.get("transactionHash", ""),
                                event_log.get("logIndex", ""),
                            )
                            if dedup_key in _seen:
                                continue
                            _seen.append(dedup_key)
                            self.events_received += 1
                            await self._process_log(event_log)
                        except json.JSONDecodeError:
                            pass
                        except Exception as e:
                            import traceback
                            logger.error(f"Relay msg error: {e}\n{traceback.format_exc()}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Frankfurt relay error: {e}. Retrying in 5s")
                await asyncio.sleep(5)

    # ── Message handling ──────────────────────────────────────────────────────

    async def _handle(self, msg: dict):
        if "result" in msg and isinstance(msg["result"], str) and msg.get("id") == 1:
            self._sub_id = msg["result"]
            logger.info(f"WhaleTracker: subscribed, sub_id={self._sub_id}")
            return

        if msg.get("method") == "eth_subscription":
            self.events_received += 1
            log = msg.get("params", {}).get("result", {})
            await self._process_log(log)

    async def _process_log(self, log: dict):
        topics   = log.get("topics", [])
        data_hex = log.get("data", "0x")[2:]
        tx_hash  = log.get("transactionHash", "")

        if len(topics) < 4 or len(data_hex) < 128:
            return

        tx_key = tx_hash + data_hex[:64]
        if tx_key in self._seen_tx:
            return
        self._seen_tx.add(tx_key)
        if len(self._seen_tx) > 10000:
            self._seen_tx = set(list(self._seen_tx)[-5000:])

        maker_addr = "0x" + topics[2][-40:]
        taker_addr = "0x" + topics[3][-40:]

        maker_is_loser = maker_addr in LOSER_ADDRESSES
        taker_is_loser = taker_addr in LOSER_ADDRESSES
        maker_is_whale = maker_addr in WHALE_ADDRESSES
        taker_is_whale = taker_addr in WHALE_ADDRESSES

        # Loser suppression: only skip if loser present AND no whale on other side
        if maker_is_loser and not taker_is_whale:
            self.loser_hits += 1
            logger.debug(f"LOSER (maker) + no whale on taker side — skip")
            return
        if taker_is_loser and not maker_is_whale:
            self.loser_hits += 1
            logger.debug(f"LOSER (taker) + no whale on maker side — skip")
            return

        whale_addr = None
        if maker_is_whale:
            whale_addr = maker_addr
        elif taker_is_whale:
            whale_addr = taker_addr
        if not whale_addr:
            return

        self.whale_hits += 1

        try:
            maker_asset  = int(data_hex[0:64],    16)
            taker_asset  = int(data_hex[64:128],  16)
            maker_amount = int(data_hex[128:192], 16)
            taker_amount = int(data_hex[192:256], 16)
        except ValueError:
            return

        if whale_addr == maker_addr:
            if maker_asset == 0:   # whale gives USDC → buying
                position_token_id = str(taker_asset)
                is_buy = True
            else:
                position_token_id = str(maker_asset)
                is_buy = False
        else:                      # whale is taker
            if taker_asset == 0:   # whale gives USDC → buying
                position_token_id = str(maker_asset)
                is_buy = True
            else:
                position_token_id = str(taker_asset)
                is_buy = False

        if position_token_id == "0":
            return
        if not is_buy:
            logger.debug(f"WhaleTracker: {WHALE_ADDRESSES.get(whale_addr)} exiting, skip")
            return

        # Market lookup
        market = self._mf.get_market_by_token(position_token_id)
        if not market:
            logger.debug(f"WhaleTracker: unknown token {position_token_id[:20]}… fetching from API")
            market = await self._mf.refresh_by_token(position_token_id)
        if not market:
            return

        seconds_left = market["end_time"] - time.time()
        if seconds_left < 30:
            logger.debug(f"WhaleTracker: market expiring in {seconds_left:.0f}s, skip")
            return

        direction  = market["direction"]
        confidence = WHALE_CONFIDENCE.get(whale_addr, 0.55)
        whale_name = WHALE_ADDRESSES[whale_addr]

        # Compute whale entry price: USDC_given / position_tokens_received
        # Both amounts are 6-decimal; ratio gives price per contract.
        try:
            if whale_addr == maker_addr and maker_asset == 0:
                # maker gives USDC, receives position tokens
                entry_price = maker_amount / taker_amount if taker_amount else 0.0
            elif whale_addr == taker_addr and taker_asset == 0:
                # taker gives USDC, receives position tokens
                entry_price = taker_amount / maker_amount if maker_amount else 0.0
            else:
                entry_price = 0.0
        except (ZeroDivisionError, TypeError):
            entry_price = 0.0

        signal = WhaleTrade(
            whale_addr   = whale_addr,
            whale_name   = whale_name,
            condition_id = market["condition_id"],
            token_id     = position_token_id,
            direction    = direction,
            symbol       = market["symbol"],
            title        = market["title"],
            confidence   = confidence,
            end_time     = market["end_time"],
            ts           = time.time(),
            tx_hash      = tx_hash,
            price        = entry_price,
        )

        self.signals_fired += 1
        logger.info(
            f"WHALE SIGNAL: {whale_name} → {direction} {market['symbol']} "
            f"conf={confidence:.2f} exp_in={seconds_left:.0f}s"
        )

        if self._on_whale_trade:
            await self._on_whale_trade(signal)

    def stop(self):
        self._running = False
        if self._ws:
            asyncio.create_task(self._ws.close())