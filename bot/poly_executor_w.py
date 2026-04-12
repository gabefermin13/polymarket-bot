"""

PolyExecutor — executes trades on Polymarket CLOB with Kelly sizing.



Paper mode: logs trades, simulates outcomes at market resolution.

Live mode:  executes FOK orders via py-clob-client, tracks real P&L.



Position tracking persists in logs/positions.jsonl.

"""

import asyncio

import json

import logging

import os

import time

from dataclasses import dataclass, field, asdict

from typing import Optional



import httpx



logger = logging.getLogger(__name__)



LOGS_DIR  = os.getenv("LOGS_DIR", "logs")

BOT_LABEL = os.getenv("BOT_LABEL", "")

_LABEL    = f"[{BOT_LABEL}] " if BOT_LABEL else ""



# Sizing / risk controls

MAX_KELLY_FRACTION   = 0.20   # quarter-Kelly for safety

MIN_CONFIDENCE       = float(os.getenv("MIN_CONFIDENCE",  "0.65"))  # env-override

MAX_CONTRACTS_PAPER  = int(os.getenv("MAX_CONTRACTS_PAPER", "50"))

MAX_CONTRACTS_LIVE   = 15

MIN_MARKET_SECS_LEFT = 28     # don't enter if < 45s to resolution

MAX_OPEN_POSITIONS   = 10     # cap simultaneous open positions

MIN_TRADE_DOLLARS    = 1.00   # skip if Kelly says bet < $1

MIN_ASK_PRICE        = float(os.getenv("MIN_ASK_PRICE",   "0.05"))  # env-override; W=0.40

MAX_ENTRY_PRICE      = float(os.getenv("MAX_ENTRY_PRICE", "1.0"))   # cap high-risk bets



# Fee estimate (Polymarket CLOB taker fee ≈ 1%)

TAKER_FEE = 0.01



# ── Correlation matrix — empirical 30-day rolling correlation (approx) ────────

# Used to discount Kelly when correlated positions are already open.

_CORR_MATRIX = {

    ("BTC",  "ETH"):  0.80,

    ("BTC",  "DOGE"): 0.60,

    ("BTC",  "XRP"):  0.65,

    ("ETH",  "DOGE"): 0.55,

    ("ETH",  "XRP"):  0.60,

    ("DOGE", "XRP"):  0.55,

}



def _asset_corr(a1: str, a2: str) -> float:

    key = tuple(sorted([a1.upper(), a2.upper()]))

    return _CORR_MATRIX.get(key, 0.30)   # default 0.30 for unlisted pairs





@dataclass

class Position:

    id:           str          # unique trade id

    condition_id: str

    token_id:     str

    direction:    str          # "Up" or "Down"

    symbol:       str

    title:        str

    source:       str          # "whale:<name>" or "direction"

    confidence:   float

    contracts:    int

    entry_price:  float        # decimal (0.0–1.0)

    cost_usd:     float        # contracts * entry_price

    end_time:     float

    ts_open:      float

    ts_close:     Optional[float] = None

    exit_price:   Optional[float] = None

    realized_pnl: Optional[float] = None

    status:       str = "open"  # "open" | "won" | "lost" | "expired"

    paper:        bool = True

    tx_hash:      Optional[str] = None





class PolyExecutor:

    """

    Manages position sizing and execution for Polymarket trades.

    """



    def __init__(self, bankroll_usd: float, paper_mode: bool = True):

        self.bankroll    = bankroll_usd

        self.paper_mode  = paper_mode

        self._positions: dict[str, Position] = {}   # id → Position

        self._http = httpx.AsyncClient(timeout=20.0)

        self._poly_client = None   # set if live mode

        os.makedirs(LOGS_DIR, exist_ok=True)



        if not paper_mode:

            self._init_live_client()



    def _reload_open_positions(self):
        """Reload open positions from disk on startup so restarts dont orphan trades."""
        positions_file = os.path.join(LOGS_DIR, "positions.jsonl")
        if not os.path.exists(positions_file):
            return
        now = time.time()
        loaded = 0
        seen_ids = {}
        try:
            with open(positions_file) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        pid = d.get("id")
                        if pid:
                            seen_ids[pid] = d
                    except Exception:
                        pass
            for pid, d in seen_ids.items():
                if d.get("event") == "open" and d.get("status") == "open":
                    end_time = d.get("end_time", 0)
                    pos = Position(
                        id           = pid,
                        condition_id = d.get("condition_id", ""),
                        token_id     = d.get("token_id", ""),
                        direction    = d.get("direction", ""),
                        symbol       = d.get("symbol", ""),
                        title        = d.get("title", ""),
                        source       = d.get("source", ""),
                        confidence   = d.get("confidence", 0.0),
                        contracts    = d.get("contracts", 0),
                        entry_price  = d.get("entry_price", 0.0),
                        cost_usd     = d.get("cost_usd", 0.0),
                        end_time     = end_time,
                        ts_open      = d.get("ts_open", 0.0),
                    )
                    self._positions[pid] = pos
                    loaded += 1
        except Exception as e:
            logger.warning(f"_reload_open_positions: {e}")
        if loaded:
            logger.info(f"Reloaded {loaded} open positions from disk")


    def _init_live_client(self):

        try:

            from py_clob_client.client import ClobClient

            from py_clob_client.clob_types import ApiCreds

            key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")

            addr = os.getenv("POLYMARKET_ADDRESS", "")

            if not key or not addr:

                logger.error("LIVE mode requires POLYMARKET_PRIVATE_KEY and POLYMARKET_ADDRESS")

                return

            self._poly_client = ClobClient(

                host           = "https://clob.polymarket.com",

                key            = key,

                chain_id       = 137,

                signature_type = 1,

                funder         = addr,

            )

            logger.info(f"PolyExecutor LIVE client initialised for {addr[:12]}…")

        except Exception as e:

            logger.error(f"Failed to init CLOB client: {e}")



    # ── Core execute ──────────────────────────────────────────────────────────



    def _corr_discount(self, asset: str, direction: str) -> float:

        """

        Kelly multiplier discount based on correlation with currently open positions.



        Same direction + high correlation = concentrated risk → larger discount.

        Opposite direction + high correlation = partial natural hedge → smaller discount.

        Returns a multiplier in [0.25, 1.0].

        """

        discount = 1.0

        for pos in self.open_positions():

            pos_asset = (pos.symbol.split("-")[0] if pos.symbol else "").upper()

            if not pos_asset or pos_asset == asset.upper():

                continue

            corr = _asset_corr(asset, pos_asset)

            if pos.direction.lower() == direction.lower():

                discount *= (1.0 - corr * 0.5)   # same direction: bigger cut

            else:

                discount *= (1.0 - corr * 0.1)   # opposite direction: small hedge benefit

        return max(discount, 0.25)   # never below 25% of original Kelly



    async def execute(

        self,

        market:           dict,

        direction:        str,

        confidence:       float,

        source:           str,

        ask_price:        Optional[float] = None,   # if None, fetched from CLOB

        kelly_multiplier: float = 1.0,              # scale Kelly (ML score, peak boost, etc.)

    ) -> tuple:

        """

        Attempt to enter a position. Returns Position on success, None if skipped.

        """

        if confidence < MIN_CONFIDENCE:

            logger.debug(f"execute: confidence {confidence:.3f} < min, skip")

            return None, "min_confidence"



        if len(self.open_positions()) >= MAX_OPEN_POSITIONS:

            logger.debug("execute: max open positions reached, skip")

            return None, "max_open_positions"



        seconds_left = market["end_time"] - time.time()

        if seconds_left < MIN_MARKET_SECS_LEFT:

            logger.debug(f"execute: only {seconds_left:.0f}s left, skip")

            return None, "too_close_to_expiry"



        # Get current ask price for the token

        token_id = (market["up_token_id"]

                    if direction == "Up"

                    else market["down_token_id"])



        if ask_price is None:

            ask_price = await self._get_best_ask(token_id)

        if ask_price is None or ask_price <= 0 or ask_price >= 1.0:

            logger.debug(f"execute: bad ask price {ask_price}, skip")

            return None, "bad_ask_price"



        # Baker longshot-bias flip: if we would buy a <30c longshot, flip to the

        # complement token (the >70c favorite) instead. Empirically validated across

        # 72M Polymarket trades — longshots are systematically overpriced.

        _LONGSHOT_THRESH = float(os.getenv("LONGSHOT_FLIP_THRESH", "0.30"))

        if ask_price < _LONGSHOT_THRESH:

            _flip_dir = "Down" if direction == "Up" else "Up"

            _flip_tok = market.get("down_token_id" if _flip_dir == "Down" else "up_token_id")

            if _flip_tok:

                _flip_ask = await self._get_best_ask(_flip_tok)

                if _flip_ask and 0 < _flip_ask < 1.0:

                    logger.info(

                        f"execute: longshot flip {direction}@{ask_price:.3f} → {_flip_dir}@{_flip_ask:.3f}"

                    )

                    direction, token_id, ask_price = _flip_dir, _flip_tok, _flip_ask



        if ask_price < MIN_ASK_PRICE:

            logger.debug(f"execute: ask {ask_price:.3f} below min {MIN_ASK_PRICE}, skip long-shot")

            return None, "below_min_ask"



        if ask_price > MAX_ENTRY_PRICE:

            logger.debug(f"execute: ask {ask_price:.3f} above max {MAX_ENTRY_PRICE:.2f}, skip high-risk")

            return None, "above_max_entry"



        # Kelly sizing — apply multiplier and correlation discount

        corr_mult = self._corr_discount(

            asset     = market.get("symbol", "").split("-")[0],

            direction = direction,

        )

        effective_mult = kelly_multiplier * corr_mult

        contracts = self._kelly_size(confidence, ask_price, multiplier=effective_mult)

        if contracts < 1:

            logger.debug(

                f"execute: Kelly=0 conf={confidence:.3f} ask={ask_price:.3f} "

                f"km={kelly_multiplier:.2f} corr={corr_mult:.2f}, skip"

            )

            return None, "kelly_zero"



        cost_usd = contracts * ask_price * (1 + TAKER_FEE)

        if cost_usd < MIN_TRADE_DOLLARS:

            logger.debug(f"execute: cost ${cost_usd:.2f} below minimum, skip")

            return None, "below_min_dollars"



        # Build position record

        pos_id = f"{market['condition_id'][:8]}-{direction}-{int(time.time())}"

        pos = Position(

            id           = pos_id,

            condition_id = market["condition_id"],

            token_id     = token_id,

            direction    = direction,

            symbol       = market["symbol"],

            title        = market["title"],

            source       = source,

            confidence   = confidence,

            contracts    = contracts,

            entry_price  = ask_price,

            cost_usd     = round(cost_usd, 4),

            end_time     = market["end_time"],

            ts_open      = time.time(),

            paper        = self.paper_mode,

        )



        if self.paper_mode:

            self._positions[pos_id] = pos

            self._log_position(pos, event="open")

            logger.info(

                f"PAPER TRADE: {direction} {market['symbol']} "

                f"@{ask_price:.3f}  {contracts} contracts  "

                f"cost=${cost_usd:.2f}  conf={confidence:.2f}  "

                f"km={effective_mult:.2f}  src={source}  exp_in={seconds_left:.0f}s"

            )
            self._update_shared_bankroll(-cost_usd)

        else:

            tx = await self._execute_live(token_id, contracts, ask_price)

            if tx is None:

                return None, "live_execution_failed"

            pos.tx_hash = tx

            self._positions[pos_id] = pos

            self._log_position(pos, event="open")

            logger.info(

                f"LIVE TRADE: {direction} {market['symbol']} "

                f"@{ask_price:.3f}  {contracts} contracts  "

                f"cost=${cost_usd:.2f}  tx={tx[:16]}…"

            )
            self._update_shared_bankroll(-cost_usd)



        return pos, None



    # ── Position resolution ───────────────────────────────────────────────────



    async def check_and_settle_expired(self):

        """

        For each expired open position, check outcome and record P&L.

        Called periodically from main.

        """

        now = time.time()

        to_settle = [

            p for p in self._positions.values()

            if p.status == "open" and p.end_time + 30 < now

        ]

        for pos in to_settle:

            await self._settle(pos)



    async def _settle(self, pos: Position):

        """Query Polymarket for final price of this token and compute P&L."""

        try:

            r = await self._http.get(

                "https://clob.polymarket.com/last-trade-price",

                params={"token_id": pos.token_id},

            )

            if r.status_code == 200:

                data = r.json()

                exit_price = float(data.get("price", 0))

            else:

                # fallback: check if redeemable via data-api

                exit_price = await self._get_exit_price_from_data_api(pos)

        except Exception as e:

            logger.warning(f"settle: price fetch failed for {pos.id}: {e}")

            exit_price = await self._get_exit_price_from_data_api(pos)



        if exit_price is None:

            logger.warning(f"settle: could not determine exit price for {pos.id}, skipping")

            return



        # P&L: (exit - entry) * contracts — fee already paid at entry

        pnl = (exit_price - pos.entry_price) * pos.contracts

        pos.exit_price   = exit_price

        pos.realized_pnl = round(pnl, 4)

        pos.ts_close     = time.time()

        pos.status       = "won" if pnl > 0 else "lost"



        self._log_position(pos, event="close")
        self._update_shared_bankroll(pos.contracts * pos.exit_price)

        logger.info(

            f"{_LABEL}SETTLED {pos.status.upper()}: {pos.direction} {pos.symbol} "

            f"entry={pos.entry_price:.3f} exit={exit_price:.3f} "

            f"pnl=${pnl:+.2f}  src={pos.source}"

        )



    async def _get_exit_price_from_data_api(self, pos: Position) -> Optional[float]:

        """Use data-api positions to check if token is redeemable at 1.0 or 0.0."""

        try:

            owner = os.getenv("POLYMARKET_ADDRESS", "")

            if not owner:

                return None

            r = await self._http.get(

                "https://data-api.polymarket.com/positions",

                params={"user": owner},

            )

            if r.status_code != 200:

                return None

            for p in r.json():

                if str(p.get("asset", "")) == pos.token_id:

                    if p.get("redeemable"):

                        return 1.0

                    cur = p.get("curPrice")

                    if cur is not None:

                        return float(cur)

        except Exception:

            pass

        return None



    # ── Live execution ────────────────────────────────────────────────────────



    async def _execute_live(self, token_id: str, contracts: int,

                            max_price: float) -> Optional[str]:

        if not self._poly_client:

            logger.error("Live client not initialised")

            return None

        try:

            loop = asyncio.get_event_loop()



            def _place():

                from py_clob_client.clob_types import MarketOrderArgs, OrderType

                args = MarketOrderArgs(

                    token_id=token_id,

                    amount=contracts,

                    side="BUY",

                    price=max_price,

                    order_type=OrderType.FOK,

                )

                return self._poly_client.create_and_post_order(args)



            result = await loop.run_in_executor(None, _place)

            tx_hash = result.get("transactionHash", "") if isinstance(result, dict) else str(result)

            return tx_hash or "live_ok"

        except Exception as e:

            logger.error(f"Live execution failed: {e}")

            return None



    # ── Sizing ────────────────────────────────────────────────────────────────



    def _kelly_size(self, confidence: float, ask_price: float, multiplier: float = 1.0) -> int:

        """

        Full Kelly fraction: f = (p*b - q) / b  where b = (1-ask)/ask

        Scaled by multiplier (ML score, peak-hour boost, correlation discount).

        Capped at MAX_KELLY_FRACTION of bankroll.

        """

        if ask_price <= 0 or ask_price >= 1.0:

            return 0

        b = (1.0 - ask_price) / ask_price  # odds

        p = confidence

        q = 1.0 - p

        kelly = (p * b - q) / b

        kelly = max(kelly, 0.0)

        kelly = min(kelly, MAX_KELLY_FRACTION)

        kelly *= max(multiplier, 0.0)



        dollars   = kelly * self.bankroll

        contracts = int(dollars / ask_price)

        max_c     = MAX_CONTRACTS_LIVE if not self.paper_mode else MAX_CONTRACTS_PAPER

        return min(contracts, max_c)



    # ── Queries ───────────────────────────────────────────────────────────────



    def open_positions(self) -> list[Position]:

        return [p for p in self._positions.values() if p.status == "open"]



    def closed_positions(self) -> list[Position]:

        return [p for p in self._positions.values() if p.status != "open"]



    def total_pnl(self) -> float:

        return sum(p.realized_pnl for p in self._positions.values()

                   if p.realized_pnl is not None)



    def win_rate(self) -> float:

        closed = self.closed_positions()

        if not closed:

            return 0.0

        wins = sum(1 for p in closed if p.status == "won")

        return wins / len(closed)



    def unrealized_pnl(self, current_prices: dict) -> float:

        total = 0.0

        for p in self.open_positions():

            cur = current_prices.get(p.token_id)

            if cur is not None:

                total += (cur - p.entry_price) * p.contracts

        return total



    def summary(self) -> dict:

        closed = self.closed_positions()

        open_p = self.open_positions()

        return {

            "open":      len(open_p),

            "closed":    len(closed),

            "won":       sum(1 for p in closed if p.status == "won"),

            "lost":      sum(1 for p in closed if p.status == "lost"),

            "win_rate":  round(self.win_rate() * 100, 1),

            "total_pnl": round(self.total_pnl(), 2),

            "bankroll":  round(self.bankroll, 2),

            "mode":      "PAPER" if self.paper_mode else "LIVE",

        }



    # ── CLOB price fetch ──────────────────────────────────────────────────────



    async def _get_best_ask(self, token_id: str) -> Optional[float]:

        try:

            r = await self._http.get(

                "https://clob.polymarket.com/book",

                params={"token_id": token_id},

            )

            if r.status_code != 200:

                return None

            data = r.json()

            asks = data.get("asks", [])

            if not asks:

                return None

            best = min(asks, key=lambda x: float(x.get("price", 1.0)))

            return float(best["price"])

        except Exception as e:

            logger.debug(f"_get_best_ask failed: {e}")

            return None



    # ── Logging ───────────────────────────────────────────────────────────────




    def _update_shared_bankroll(self, delta: float):
        """Atomically update shared_bankroll.json and self.bankroll."""
        import fcntl
        br_path   = "/root/shared_bankroll.json"
        lock_path = "/root/shared_bankroll.json.lock"
        try:
            with open(lock_path, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                try:
                    with open(br_path) as f:
                        data = json.load(f)
                except Exception:
                    data = {"balance": self.bankroll, "initial_balance": self.bankroll}
                data["balance"] = round(data["balance"] + delta, 4)
                with open(br_path, "w") as f:
                    json.dump(data, f)
                self.bankroll = data["balance"]
        except Exception as e:
            logger.warning(f"bankroll update failed: {e}")

    def _log_position(self, pos: Position, event: str):

        record = {**asdict(pos), "event": event}

        try:

            with open(f"{LOGS_DIR}/positions.jsonl", "a", encoding="utf-8") as f:

                f.write(json.dumps(record) + "\n")

        except Exception as e:

            logger.warning(f"position log write failed: {e}")



    async def close(self):

        await self._http.aclose()