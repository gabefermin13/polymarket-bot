"""
MarketFinder — discovers and tracks active Polymarket Up-or-Down markets.

Periodically fetches from gamma-api and maintains two indices:
  token_id  → market dict
  condition_id → market dict

A "market dict" has:
  condition_id, title, symbol (BTC/ETH/SOL/DOGE/XRP), direction (Up/Down),
  up_token_id, down_token_id, end_time (epoch float), slug

Discovery strategy:
  1. Slug-based: generate slug candidates for upcoming 5-min/30-min/4-hr windows
  2. Paginated scan: fallback full fetch from gamma-api
  3. On-the-fly: refresh_by_token() for unknown tokens seen in whale trades
"""
import asyncio
import logging
import math
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GAMMA_URL     = "https://gamma-api.polymarket.com/markets"
REFRESH_SEC   = 120   # re-fetch every 2 minutes

DIRECTION_KEYWORDS = ["Up or Down", "up or down", "up-or-down", "updown",
                      "Up Or Down", "UP OR DOWN"]

SYMBOL_MAP = {
    "Bitcoin":  "BTC-USD",
    "Ethereum": "ETH-USD",
    "Solana":   "SOL-USD",
    "Dogecoin": "DOGE-USD",
    "XRP":      "XRP-USD",
    "BTC":      "BTC-USD",
    "ETH":      "ETH-USD",
    "SOL":      "SOL-USD",
    "DOGE":     "DOGE-USD",
}

# slug prefix → exchange symbol
SLUG_PREFIXES = {
    "btc":  "BTC-USD",
    "eth":  "ETH-USD",
    "sol":  "SOL-USD",
    "doge": "DOGE-USD",
    "xrp":  "XRP-USD",
}

# (interval_seconds, suffix) for slug generation
# Only 5-min and 15-min windows — 30m/4h removed (capital recycling + no edge data)
SLUG_INTERVALS = [
    (300,  "5m"),
    (900,  "15m"),
]


def _parse_end_time(market: dict) -> float:
    """Return end time as epoch float, or 0 if unknown.

    endDateIso may be just a date ("2026-04-03") for short-duration markets.
    endDate is always a full ISO-8601 datetime. Prefer the field that has time.
    """
    raw_iso  = market.get("endDateIso") or ""
    raw_date = market.get("endDate")    or ""
    # Use endDateIso only when it contains time info (has 'T'); otherwise use endDate
    raw = (raw_iso if "T" in raw_iso else None) or raw_date or raw_iso
    if not raw:
        return 0.0
    try:
        from datetime import datetime, timezone
        raw = raw.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        return 0.0


def _extract_symbol(title: str) -> Optional[str]:
    for keyword, sym in SYMBOL_MAP.items():
        if keyword.lower() in title.lower():
            return sym
    return None


def _extract_symbol_from_slug(slug: str) -> Optional[str]:
    for prefix, sym in SLUG_PREFIXES.items():
        if slug.startswith(prefix + "-"):
            return sym
    return None


def _parse_market(m: dict) -> Optional[dict]:
    """
    Convert raw gamma-api market dict into our internal format.
    Returns None if the market is not a usable Up-or-Down market.
    """
    title = m.get("question") or m.get("title") or ""
    slug  = m.get("slug", "")

    # Check direction keyword in title OR slug
    is_updown = (
        any(kw.lower() in title.lower() for kw in DIRECTION_KEYWORDS)
        or "updown" in slug.lower()
        or "up-down" in slug.lower()
    )
    if not is_updown:
        return None

    symbol = _extract_symbol(title) or _extract_symbol_from_slug(slug)
    if not symbol:
        return None

    condition_id = m.get("conditionId", "").lower()
    token_ids    = m.get("clobTokenIds", [])
    outcomes     = m.get("outcomes", [])
    end_time     = _parse_end_time(m)

    # gamma-api returns clobTokenIds as a JSON string, not a list
    if isinstance(token_ids, str):
        import json as _json
        try:
            token_ids = _json.loads(token_ids)
        except Exception:
            token_ids = []

    # outcomes may also be a JSON string
    if isinstance(outcomes, str):
        import json as _json
        try:
            outcomes = _json.loads(outcomes)
        except Exception:
            outcomes = []

    if len(token_ids) < 2 or not condition_id:
        return None

    # Skip already-expired markets
    if end_time and end_time < time.time() - 60:
        return None

    up_idx, down_idx = 0, 1
    if len(outcomes) >= 2:
        if outcomes[0].lower() in ("down", "no"):
            up_idx, down_idx = 1, 0

    up_token   = str(token_ids[up_idx])
    down_token = str(token_ids[down_idx])

    return {
        "condition_id":  condition_id,
        "title":         title,
        "symbol":        symbol,
        "up_token_id":   up_token,
        "down_token_id": down_token,
        "end_time":      end_time,
        "slug":          slug,
        "liquidity":     float(m.get("liquidityNum", 0) or 0),
    }


class MarketFinder:
    """
    Maintains a live index of active Up-or-Down binary markets.
    Refreshes in background; safe to query from any coroutine.
    """

    def __init__(self):
        self._by_token: dict[str, dict] = {}   # token_id  → market (with direction)
        self._by_cond:  dict[str, dict] = {}   # condition_id → market
        self._http = httpx.AsyncClient(timeout=30.0)
        self._running = False
        self.last_refresh = 0.0
        self.markets_loaded = 0

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_market_by_token(self, token_id: str) -> Optional[dict]:
        return self._by_token.get(str(token_id))

    def get_market_by_condition(self, condition_id: str) -> Optional[dict]:
        return self._by_cond.get(condition_id.lower())

    def get_active_markets(self, symbol: Optional[str] = None,
                           min_seconds_remaining: float = 30.0,
                           min_liquidity: float = 0.0) -> list[dict]:
        """Return markets that haven't expired yet and meet liquidity threshold."""
        now = time.time()
        seen = set()
        result = []
        for m in self._by_cond.values():
            cid = m["condition_id"]
            if cid in seen:
                continue
            seen.add(cid)
            if m["end_time"] > now + min_seconds_remaining:
                if symbol is None or m["symbol"] == symbol:
                    if m.get("liquidity", 0.0) >= min_liquidity:
                        result.append(m)
        result.sort(key=lambda x: x["end_time"])
        return result

    # ── On-the-fly token lookup ───────────────────────────────────────────────

    async def refresh_by_token(self, token_id: str) -> Optional[dict]:
        """
        Fetch a specific market by its CLOB token ID and add to index.
        Called from whale_tracker when it sees an unknown token.
        """
        try:
            r = await self._http.get(GAMMA_URL, params={"clob_token_ids": token_id})
            r.raise_for_status()
            data = r.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            self._index_markets(markets)
        except Exception as e:
            logger.debug(f"refresh_by_token({token_id[:16]}…): {e}")
        return self._by_token.get(str(token_id))

    # ── Refresh ───────────────────────────────────────────────────────────────

    async def refresh(self):
        """Fetch all active Up-or-Down markets and rebuild indices."""
        new_by_token: dict[str, dict] = {}
        new_by_cond:  dict[str, dict] = {}

        # 1. Slug-based discovery (fastest, most accurate for current markets)
        slug_markets = await self._discover_by_slugs()
        for entry in slug_markets:
            _add_entry(entry, new_by_token, new_by_cond)

        # 2. Paginated gamma-api scan (catches any we missed)
        raw_markets = await self._fetch_all()
        for m in raw_markets:
            entry = _parse_market(m)
            if entry:
                _add_entry(entry, new_by_token, new_by_cond)

        self._by_token = new_by_token
        self._by_cond  = new_by_cond
        self.last_refresh  = time.time()
        # Only count markets that are still in the future
        now = time.time()
        self.markets_loaded = sum(
            1 for m in self._by_cond.values() if m["end_time"] > now
        )
        logger.info(
            f"MarketFinder: {self.markets_loaded} active Up-or-Down markets, "
            f"{len(self._by_token)} token entries "
            f"(total in index: {len(self._by_cond)})"
        )

    def _index_markets(self, raw_list: list[dict]):
        """Parse and add markets to the live index (for on-the-fly updates)."""
        for m in raw_list:
            entry = _parse_market(m)
            if entry:
                _add_entry(entry, self._by_token, self._by_cond)

    # ── Slug-based discovery ──────────────────────────────────────────────────

    async def _discover_by_slugs(self) -> list[dict]:
        """
        Generate slug candidates for current/upcoming time windows and fetch them.
        Slug format: {sym}-updown-{interval}-{start_ts}
        e.g. btc-updown-5m-1775215200  (market starts at 1775215200, ends 5 min later)
        """
        now = time.time()
        slugs: list[str] = []

        for sym_prefix in SLUG_PREFIXES:
            for interval_sec, suffix in SLUG_INTERVALS:
                base = math.ceil(now / interval_sec) * interval_sec
                # Enough slots to cover ~1 hour ahead
                n_slots = max(6, int(3600 / interval_sec))
                for i in range(n_slots):
                    ts = int(base + i * interval_sec)
                    slugs.append(f"{sym_prefix}-updown-{suffix}-{ts}")

        results: list[dict] = []
        sem = asyncio.Semaphore(10)  # limit concurrency to avoid connection pool exhaustion

        async def fetch_slug(slug: str):
            async with sem:
                try:
                    r = await self._http.get(GAMMA_URL, params={"slug": slug})
                    if r.status_code == 200:
                        data = r.json()
                        ms = data if isinstance(data, list) else data.get("data", [])
                        for m in ms:
                            entry = _parse_market(m)
                            if entry:
                                results.append(entry)
                except Exception:
                    pass

        await asyncio.gather(*[fetch_slug(s) for s in slugs], return_exceptions=True)
        logger.debug(f"MarketFinder slug discovery: tried {len(slugs)} slugs, "
                     f"found {len(results)} markets")
        return results

    async def _fetch_all(self) -> list[dict]:
        markets = []
        next_cursor = ""
        pages = 0
        while True:
            params = {"active": "true", "closed": "false", "limit": 500}
            if next_cursor:
                params["next_cursor"] = next_cursor
            try:
                r = await self._http.get(GAMMA_URL, params=params)
                r.raise_for_status()
                data = r.json()
            except Exception as e:
                logger.error(f"MarketFinder fetch error: {e}. "
                             f"Retained {len(markets)} markets.")
                break
            if isinstance(data, list):
                batch = data
                next_cursor = ""
            else:
                batch = data.get("data", []) or []
                next_cursor = data.get("next_cursor", "") or ""
            markets.extend(batch)
            pages += 1
            if not next_cursor or len(batch) < 500 or pages > 40:
                break
        logger.debug(f"MarketFinder: fetched {len(markets)} total markets in {pages} pages")
        return markets

    # ── Background refresh loop ───────────────────────────────────────────────

    async def run_refresh_loop(self):
        self._running = True
        while self._running:
            try:
                await self.refresh()
            except Exception as e:
                logger.error(f"MarketFinder refresh error: {e}")
            try:
                await asyncio.sleep(REFRESH_SEC)
            except asyncio.CancelledError:
                break

    async def close(self):
        self._running = False
        await self._http.aclose()


# ── Module-level helpers ──────────────────────────────────────────────────────

def _add_entry(entry: dict,
               by_token: dict[str, dict],
               by_cond:  dict[str, dict]):
    up   = entry["up_token_id"]
    down = entry["down_token_id"]
    cid  = entry["condition_id"]
    by_token[up]   = {**entry, "direction": "Up",   "token_id": up}
    by_token[down] = {**entry, "direction": "Down",  "token_id": down}
    by_cond[cid]   = entry
