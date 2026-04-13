#!/usr/bin/env python3
"""
fetch_econ_calendar.py -- Hourly cron script.
Fetches Forex Factory High-impact USD events and writes to /root/economic_calendar.json.
Run every hour via cron: 0 * * * * python3 /root/fetch_econ_calendar.py
"""
import json, time, sys
import urllib.request

FF_URL   = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
OUT_FILE = "/root/economic_calendar.json"

try:
    req = urllib.request.Request(FF_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = json.loads(r.read())
    events = [e for e in raw if e.get("country") == "USD" and e.get("impact") == "High"]
    payload = {"fetched_at": time.time(), "events": events}
    with open(OUT_FILE, "w") as f:
        json.dump(payload, f)
    print(f"[econ_calendar] wrote {len(events)} events to {OUT_FILE}")
except Exception as e:
    print(f"[econ_calendar] fetch failed: {e}", file=sys.stderr)
    sys.exit(1)
