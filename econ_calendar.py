"""
econ_calendar.py — Economic calendar blackout gate.

Fetches Forex Factory weekly calendar (free JSON feed, no auth required).
Filters to High-impact USD events only and blocks trading within a window
around each release.

Blocked events include: NFP, CPI, FOMC rate decision, PCE, PPI, GDP, ISM,
Retail Sales, JOLTS, ADP. These cause violent short-term price noise that
destroys signal quality.

Usage:
    cal = EconCalendar()
    if await cal.is_blackout(client):
        # skip trade — macro release imminent or just occurred
"""
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional

import httpx

logger = logging.getLogger("econ_calendar")

_FF_URL      = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
_CACHE_TTL   = 14400   # re-fetch every 4 hours
_BEFORE_MINS = 10      # blackout starts N minutes BEFORE release
_AFTER_MINS  = 5       # blackout ends N minutes AFTER release


class EconCalendar:
    def __init__(self, before_mins: int = _BEFORE_MINS, after_mins: int = _AFTER_MINS):
        self._before_secs  = before_mins * 60
        self._after_secs   = after_mins  * 60
        self._events:      List[dict] = []
        self._fetched_at:  float = 0.0

    async def _refresh(self, client: httpx.AsyncClient):
        now = time.time()
        if now - self._fetched_at < _CACHE_TTL and self._events:
            return
        try:
            r = await client.get(_FF_URL, timeout=10.0)
            if r.status_code == 200:
                raw = r.json()
                self._events    = [
                    e for e in raw
                    if e.get("country") == "USD" and e.get("impact") == "High"
                ]
                self._fetched_at = now
                logger.info(f"EconCalendar: {len(self._events)} high-impact USD events loaded")
            else:
                logger.warning(f"EconCalendar: HTTP {r.status_code} — no blackout gate applied")
        except Exception as e:
            logger.warning(f"EconCalendar fetch failed: {e} — no blackout gate applied")

    def _parse_event_ts(self, event: dict) -> Optional[float]:
        """Parse Forex Factory date+time strings → UTC epoch.
        FF times are US/Eastern. Approximates EDT (UTC-4) for most of the year."""
        try:
            date_str = event.get("date", "")   # "2026-04-04"
            time_str = event.get("time", "")   # "8:30am" | "Tentative" | ""
            if not date_str or not time_str:
                return None
            if time_str.lower() in ("tentative", "all day", ""):
                return None
            dt_str   = f"{date_str} {time_str.upper()}"
            # Try both 12-hour formats: "8:30AM" and "8:30 AM"
            for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p"):
                try:
                    dt_local = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
            # EDT = UTC-4 (valid for DST period covering most economic releases)
            dt_utc = dt_local + timedelta(hours=4)
            return dt_utc.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None

    async def is_blackout(self, client: httpx.AsyncClient) -> bool:
        """
        Returns True if current time is within [before_mins before, after_mins after]
        any High-impact USD event. Caller should skip trade if True.
        Fails open — if calendar unavailable, returns False (don't block).
        """
        await self._refresh(client)
        if not self._events:
            return False

        now = time.time()
        for event in self._events:
            ev_ts = self._parse_event_ts(event)
            if ev_ts is None:
                continue
            if (ev_ts - self._before_secs) <= now <= (ev_ts + self._after_secs):
                title   = event.get("title", "unknown event")
                ev_time = datetime.utcfromtimestamp(ev_ts).strftime("%H:%M UTC")
                logger.info(f"[EconCalendar] BLACKOUT: {title} @ {ev_time}")
                return True
        return False
