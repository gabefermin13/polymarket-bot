"""
econ_calendar.py -- Economic calendar blackout gate.

Reads from local cache file /root/economic_calendar.json (written hourly by
fetch_econ_calendar.py cron). No live API call in the trade path.

Blocked events: NFP, CPI, FOMC, PCE, PPI, GDP, ISM, Retail Sales, JOLTS, ADP.

Usage:
    cal = EconCalendar()
    if cal.is_blackout():
        # skip trade
"""
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("econ_calendar")

_CACHE_FILE  = Path("/root/economic_calendar.json")
_BEFORE_SECS = 10 * 60   # 10 min before
_AFTER_SECS  =  5 * 60   #  5 min after
_STALE_WARN  = 7200       # warn if cache older than 2h


class EconCalendar:
    def __init__(self):
        self._events: List[dict] = []
        self._loaded_at: float = 0.0
        self._load()

    def _load(self):
        """Load events from local cache file. Call once at startup + periodically."""
        try:
            if not _CACHE_FILE.exists():
                logger.warning("EconCalendar: cache file not found -- no blackout gate")
                return
            with open(_CACHE_FILE) as f:
                data = json.load(f)
            self._events    = data.get("events", [])
            self._loaded_at = data.get("fetched_at", 0.0)
            age = time.time() - self._loaded_at
            if age > _STALE_WARN:
                logger.warning(f"EconCalendar: cache is {age/3600:.1f}h old -- cron may be broken")
            else:
                logger.info(f"EconCalendar: {len(self._events)} events loaded (cache age {age/60:.0f}m)")
        except Exception as e:
            logger.warning(f"EconCalendar: load failed: {e} -- no blackout gate")
            self._events = []

    def refresh_if_stale(self, max_age_secs: int = 3600):
        """Call this occasionally (e.g. each scan cycle) to reload cache if updated."""
        if time.time() - self._loaded_at > max_age_secs:
            self._load()

    def _parse_event_ts(self, event: dict) -> Optional[float]:
        try:
            date_str = event.get("date", "")
            if not date_str:
                return None
            # ISO format from fetch_econ_calendar.py: "2026-04-14T08:30:00-04:00"
            if "T" in date_str:
                try:
                    dt = datetime.fromisoformat(date_str)
                    return dt.astimezone(timezone.utc).timestamp()
                except Exception:
                    pass
            # Legacy format: separate date + time fields
            time_str = event.get("time", "")
            if not time_str or time_str.lower() in ("tentative", "all day", ""):
                return None
            dt_str = f"{date_str} {time_str.upper()}"
            for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p"):
                try:
                    dt_local = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
            dt_utc = dt_local + timedelta(hours=4)
            return dt_utc.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None

    def _check_blackout(self) -> bool:
        """
        Returns True if current time is within blackout window of any event.
        Synchronous -- reads only from in-memory cache. No I/O, no latency.
        Fails open (returns False) if cache is empty.
        """
        if not self._events:
            return False
        now = time.time()
        for event in self._events:
            ev_ts = self._parse_event_ts(event)
            if ev_ts is None:
                continue
            if (ev_ts - _BEFORE_SECS) <= now <= (ev_ts + _AFTER_SECS):
                title   = event.get("title", "unknown")
                ev_time = datetime.utcfromtimestamp(ev_ts).strftime("%H:%M UTC")
                logger.info(f"[EconCalendar] BLACKOUT: {title} @ {ev_time}")
                return True
        return False

    # Async signature -- d_main calls `await econ_cal.is_blackout(client)`
    # No actual I/O needed; refreshes from local cache if stale.
    async def is_blackout(self, client=None) -> bool:
        self.refresh_if_stale()
        return self._check_blackout()

    # Synchronous alias for callers that don't use await
    def is_blackout_sync(self) -> bool:
        self.refresh_if_stale()
        return self._check_blackout()
