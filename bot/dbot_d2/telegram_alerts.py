"""
Telegram alerts for yes/no arbitrage scanner.
"""
import asyncio
import logging
import os
import time
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
BASE    = f"https://api.telegram.org/bot{TOKEN}"


class TelegramAlerts:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=30.0)
        self._last_update_id = 0
        self._commands: dict[str, Callable] = {}

    async def send(self, text: str) -> bool:
        if not TOKEN or not CHAT_ID:
            logger.debug("Telegram not configured, skipping")
            return False
        try:
            resp = await self._http.post(
                f"{BASE}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            )
            if resp.status_code == 400:
                resp = await self._http.post(
                    f"{BASE}/sendMessage",
                    json={"chat_id": CHAT_ID, "text": text},
                )
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")
            return False

    def register_command(self, cmd: str, handler: Callable):
        self._commands[cmd] = handler

    async def poll_commands(self):
        logger.info("Telegram polling started")
        while True:
            try:
                resp = await self._http.get(
                    f"{BASE}/getUpdates",
                    params={"offset": self._last_update_id + 1, "timeout": 30},
                    timeout=40.0,
                )
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(5)
                    continue
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip()
                    if text.startswith("/"):
                        cmd = text.split()[0].lower()
                        handler = self._commands.get(cmd)
                        if handler:
                            try:
                                reply = await handler()
                                if reply:
                                    await self.send(reply)
                            except Exception as e:
                                await self.send(f"Error in {cmd}: {e}")
                        else:
                            await self.send(f"Unknown command: {cmd}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Telegram poll error: {e}")
                await asyncio.sleep(10)

    async def send_gap_alert(self, ticker: str, yes_ask: int, no_ask: int,
                              gap_cents: int, exe_size: int):
        await self.send(
            f"*GAP DETECTED*\n"
            f"Ticker: `{ticker}`\n"
            f"YES ask: `{yes_ask}c`  NO ask: `{no_ask}c`\n"
            f"Gap: `{gap_cents}c`  Exe size: `{exe_size}`"
        )

    async def send_fill_alert(self, ticker: str, outcome: str, contracts: int,
                               gap_cents: int, profit_cents: int):
        emoji = {"BOTH_FILLED": "✅", "PARTIAL_FILL_YES_ONLY": "⚠️",
                 "PARTIAL_FILL_NO_ONLY": "⚠️", "MISS": "❌"}.get(outcome, "?")
        await self.send(
            f"{emoji} *PAPER FILL*  `{outcome}`\n"
            f"Ticker: `{ticker}`\n"
            f"Contracts: `{contracts}`  Gap: `{gap_cents}c`\n"
            f"Conservative profit: `${profit_cents/100:.4f}`"
        )

    async def send_startup(self, n_markets: int, balance: float, live_mode: bool,
                            latency_ms: int, min_gap: int):
        mode = "LIVE" if live_mode else "PAPER"
        await self.send(
            f"*YES+NO ARB SCANNER STARTED*  `[{mode}]`\n"
            f"Markets monitored : `{n_markets}`\n"
            f"Balance           : `${balance:.2f}`\n"
            f"Latency assumption: `{latency_ms}ms`\n"
            f"Min gap to enter  : `{min_gap}c`"
        )

    async def send_shutdown_summary(self, summary: dict):
        s = summary
        await self.send(
            f"*SESSION SUMMARY*\n"
            f"Gaps closed : `{s['gap_opportunities_closed']}`\n"
            f"Both filled : `{s['theoretical_both_fill']}` ({s['both_fill_rate_pct']}%)\n"
            f"Miss rate   : `{s['miss_rate_pct']}%`\n"
            f"Theo profit : `${s['theoretical_locked_profit_dollars']:.4f}`\n"
            f"Cons profit : `${s['conservative_locked_profit_dollars']:.4f}`"
        )

    async def close(self):
        await self._http.aclose()
