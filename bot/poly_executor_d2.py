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
from decimal import Decimal, ROUND_HALF_UP

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

MIN_ASK_PRICE        = float(os.getenv("MIN_ASK_PRICE",   "0.05"))  # env-override; D2=0.45

MAX_ENTRY_PRICE      = float(os.getenv("MAX_ENTRY_PRICE", "1.0"))   # cap high-risk bets



# Fee estimate (Polymarket CLOB taker fee ≈ 1%)

TAKER_FEE = 0.01

POLY_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
POLY_SIGNATURE_TYPE = 1



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

    contracts:    float

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
        self._settle_skip_log_ts: dict[tuple[str, str], float] = {}

        self._http = httpx.AsyncClient(timeout=20.0)

        self._poly_client = None   # set if live mode

        os.makedirs(LOGS_DIR, exist_ok=True)



        if not paper_mode:

            self._init_live_client()
            self._sync_shared_bankroll_to_wallet()



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
                    if end_time and end_time + 30 < now:
                        continue
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

    def _get_live_wallet_usdc_balance(self) -> Optional[float]:
        owner = self._live_owner()
        if not owner:
            return None
        usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        call_data = "0x70a08231" + owner.lower().replace("0x", "").rjust(64, "0")
        rpc_urls = [
            os.getenv("POLYGON_RPC_URL", ""),
            os.getenv("RPC_URL", ""),
            "https://polygon-bor-rpc.publicnode.com",
        ]
        for rpc_url in rpc_urls:
            if not rpc_url:
                continue
            try:
                resp = httpx.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_call",
                        "params": [
                            {"to": usdc, "data": call_data},
                            "latest",
                        ],
                    },
                    timeout=20.0,
                )
                resp.raise_for_status()
                result = resp.json().get("result", "0x0")
                return round(int(result, 16) / 1_000_000, 6)
            except Exception:
                continue
        return None

    def _sync_shared_bankroll_to_wallet(self):
        wallet_balance = self._get_live_wallet_usdc_balance()
        if wallet_balance is None:
            return
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
                    data = {"balance": wallet_balance, "initial_balance": wallet_balance}
                data["balance"] = round(wallet_balance, 4)
                with open(br_path, "w") as f:
                    json.dump(data, f)
                self.bankroll = data["balance"]
        except Exception as e:
            logger.warning(f"bankroll sync failed: {e}")

    def _log_settle_skip(self, pos_id: str, reason: str):
        now = time.time()
        key = (pos_id, reason)
        last = self._settle_skip_log_ts.get(key, 0.0)
        if now - last >= 60.0:
            logger.info(f"settle: no confirmed {reason} activity yet for {pos_id}, skipping")
            self._settle_skip_log_ts[key] = now

    def _resolve_proxy_funder(self, signer_addr: str, fallback_addr: str) -> str:
        try:
            from eth_utils import keccak

            selector = keccak(text="getPolyProxyWalletAddress(address)")[:4].hex()
            call_data = "0x" + selector + signer_addr.lower().replace("0x", "").rjust(64, "0")
            rpc_urls = [
                os.getenv("POLYGON_RPC_URL", ""),
                os.getenv("RPC_URL", ""),
                "https://polygon-bor-rpc.publicnode.com",
            ]
            for rpc_url in rpc_urls:
                if not rpc_url:
                    continue
                try:
                    resp = httpx.post(
                        rpc_url,
                        json={
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "eth_call",
                            "params": [
                                {"to": POLY_CTF_EXCHANGE, "data": call_data},
                                "latest",
                            ],
                        },
                        timeout=20.0,
                    )
                    resp.raise_for_status()
                    result = resp.json().get("result", "")
                    if isinstance(result, str) and len(result) == 66:
                        return "0x" + result[-40:]
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"proxy funder resolution failed: {e}")
        return fallback_addr

    def _live_owner(self) -> str:
        if self._poly_client and getattr(self._poly_client, "builder", None):
            owner = getattr(self._poly_client.builder, "funder", "")
            if owner:
                return owner
        return os.getenv("POLYMARKET_ADDRESS", "")

    async def _get_recent_live_trade_data(
        self, token_id: str, side: str, since_ts: float
    ) -> Optional[dict]:
        owner = self._live_owner()
        if not owner:
            return None
        try:
            r = await self._http.get(
                "https://data-api.polymarket.com/activity",
                params={"user": owner, "limit": 200},
            )
            if r.status_code != 200:
                return None
            fills = [
                x for x in r.json()
                if x.get("type") == "TRADE"
                and str(x.get("asset", "")) == str(token_id)
                and str(x.get("side", "")).upper() == side.upper()
                and float(x.get("timestamp", 0) or 0) >= since_ts - 1
            ]
            if not fills:
                return None
            fills.sort(key=lambda x: (float(x.get("timestamp", 0) or 0), x.get("transactionHash", "")))
            usdc_spent = sum(float(x.get("usdcSize", 0) or 0) for x in fills)
            filled_size = sum(float(x.get("size", 0) or 0) for x in fills)
            tx_hashes = [x.get("transactionHash", "") for x in fills if x.get("transactionHash")]
            latest_ts = max(float(x.get("timestamp", 0) or 0) for x in fills)
            return {
                "cost_usd": round(usdc_spent, 6),
                "contracts": round(filled_size, 6),
                "entry_price": round(usdc_spent / filled_size, 6) if filled_size else 0.0,
                "tx_hash": ",".join(dict.fromkeys(tx_hashes)) or "live_ok",
                "latest_ts": latest_ts,
            }
        except Exception as e:
            logger.warning(f"live trade reconciliation fetch failed: {e}")
            return None

    async def _await_live_trade_data(
        self, token_id: str, side: str, since_ts: float, timeout_secs: float = 60.0
    ) -> Optional[dict]:
        deadline = time.time() + timeout_secs
        while time.time() < deadline:
            data = await self._get_recent_live_trade_data(token_id, side, since_ts)
            if data and float(data.get("contracts", 0) or 0) > 0:
                return data
            await asyncio.sleep(2)
        return await self._get_recent_live_trade_data(token_id, side, since_ts)

    async def _get_confirmed_settlement_data(self, pos: Position) -> Optional[dict]:
        owner = self._live_owner()
        if not owner:
            return None
        try:
            r = await self._http.get(
                "https://data-api.polymarket.com/activity",
                params={"user": owner, "limit": 200},
            )
            if r.status_code != 200:
                return None
            exits = []
            for x in r.json():
                if str(x.get("asset", "")) != pos.token_id:
                    continue
                if float(x.get("timestamp", 0) or 0) < pos.ts_open - 1:
                    continue
                is_redeem = x.get("type") == "REDEEM"
                is_sell = x.get("type") == "TRADE" and str(x.get("side", "")).upper() == "SELL"
                if is_redeem or is_sell:
                    exits.append(x)
            if not exits:
                return None
            exits.sort(key=lambda x: (float(x.get("timestamp", 0) or 0), x.get("transactionHash", "")))
            payout_usd = sum(float(x.get("usdcSize", 0) or 0) for x in exits)
            exit_size = sum(float(x.get("size", 0) or 0) for x in exits)
            tx_hashes = [x.get("transactionHash", "") for x in exits if x.get("transactionHash")]
            latest_ts = max(float(x.get("timestamp", 0) or 0) for x in exits)
            pnl = payout_usd - pos.cost_usd
            if exit_size > 0:
                exit_price = payout_usd / exit_size
            elif payout_usd > 0:
                exit_price = payout_usd / pos.contracts if pos.contracts else 0.0
            else:
                exit_price = 0.0
            return {
                "exit_price": round(exit_price, 6),
                "payout_usd": round(payout_usd, 6),
                "realized_pnl": round(pnl, 4),
                "status": "won" if pnl > 0 else "lost",
                "tx_hash": ",".join(dict.fromkeys(tx_hashes)) or pos.tx_hash,
                "ts_close": latest_ts,
            }
        except Exception as e:
            logger.warning(f"settlement activity fetch failed for {pos.id}: {e}")
            return None

    async def _get_confirmed_entry_data(self, pos: Position) -> Optional[dict]:
        entry = await self._get_recent_live_trade_data(pos.token_id, "BUY", pos.ts_open)
        if not entry:
            return None
        if float(entry.get("contracts", 0) or 0) <= 0 or float(entry.get("cost_usd", 0) or 0) <= 0:
            return None
        return entry


    def _init_live_client(self):

        try:

            from py_clob_client.client import ClobClient

            from py_clob_client.clob_types import ApiCreds
            from eth_account import Account

            key  = os.getenv("POLYMARKET_PRIVATE_KEY", "")

            addr = os.getenv("POLYMARKET_ADDRESS", "")

            if not key or not addr:

                logger.error("LIVE mode requires POLYMARKET_PRIVATE_KEY and POLYMARKET_ADDRESS")

                return

            signer_addr = Account.from_key(key).address
            funder_addr = self._resolve_proxy_funder(signer_addr, addr)
            if funder_addr.lower() != addr.lower():
                logger.warning(
                    f"POLYMARKET_ADDRESS {addr} does not match proxy maker for signer "
                    f"{signer_addr}; using {funder_addr}"
                )

            self._poly_client = ClobClient(

                host           = "https://clob.polymarket.com",

                key            = key,

                chain_id       = 137,

                signature_type = POLY_SIGNATURE_TYPE,

                funder         = funder_addr,

            )

            creds = self._poly_client.create_or_derive_api_creds()
            self._poly_client.set_api_creds(creds)
            logger.info(
                f"PolyExecutor LIVE client initialised for maker={funder_addr} signer={signer_addr}"
            )

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



        pos_id = f"{market['condition_id'][:8]}-{direction}-{int(time.time())}"



        if self.paper_mode:
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

            live_fill = await self._execute_live(token_id, contracts, ask_price)

            if live_fill is None:

                return None, "live_execution_failed"

            pos = Position(

                id           = pos_id,

                condition_id = market["condition_id"],

                token_id     = token_id,

                direction    = direction,

                symbol       = market["symbol"],

                title        = market["title"],

                source       = source,

                confidence   = confidence,

                contracts    = float(live_fill["contracts"]),

                entry_price  = float(live_fill["entry_price"]),

                cost_usd     = round(float(live_fill["cost_usd"]), 4),

                end_time     = market["end_time"],

                ts_open      = float(live_fill.get("ts_open", time.time())),

                paper        = self.paper_mode,

                tx_hash      = live_fill["tx_hash"],

            )

            self._positions[pos_id] = pos

            self._log_position(pos, event="open")

            logger.info(

                f"LIVE TRADE: {direction} {market['symbol']} "

                f"@{pos.entry_price:.3f}  {pos.contracts} contracts  "

                f"cost=${pos.cost_usd:.2f}  tx={pos.tx_hash[:16]}…"

            )
            self._update_shared_bankroll(-pos.cost_usd)



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

        """Only settle after Polymarket shows a confirmed cash exit."""

        entry = await self._get_confirmed_entry_data(pos)
        if entry is None:
            self._log_settle_skip(pos.id, "entry")
            return
        pos.contracts = float(entry["contracts"])
        pos.entry_price = float(entry["entry_price"])
        pos.cost_usd = round(float(entry["cost_usd"]), 4)
        pos.tx_hash = str(entry.get("tx_hash") or pos.tx_hash)

        settlement = await self._get_confirmed_settlement_data(pos)
        if settlement is None:
            self._log_settle_skip(pos.id, "exit")
            return

        pos.exit_price   = float(settlement["exit_price"])

        pos.realized_pnl = float(settlement["realized_pnl"])

        pos.ts_close     = float(settlement["ts_close"])

        pos.status       = str(settlement["status"])
        pos.tx_hash      = str(settlement.get("tx_hash") or pos.tx_hash)
        self._settle_skip_log_ts.pop((pos.id, "entry"), None)
        self._settle_skip_log_ts.pop((pos.id, "exit"), None)



        self._log_position(pos, event="close")
        self._update_shared_bankroll(float(settlement["payout_usd"]))

        logger.info(

            f"{_LABEL}SETTLED {pos.status.upper()}: {pos.direction} {pos.symbol} "

            f"entry={pos.entry_price:.3f} exit={pos.exit_price:.3f} "

            f"pnl=${pos.realized_pnl:+.2f}  src={pos.source}"

        )



    async def _get_exit_price_from_data_api(self, pos: Position) -> Optional[float]:

        """Use data-api positions to check if token is redeemable at 1.0 or 0.0."""

        try:

            owner = self._live_owner()

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

                            max_price: float) -> Optional[dict]:

        if not self._poly_client:

            logger.error("Live client not initialised")

            return None

        try:

            loop = asyncio.get_event_loop()
            submit_ts = time.time()



            def _place():

                from py_clob_client.clob_types import MarketOrderArgs, OrderType
                from py_clob_client.config import get_contract_config
                from py_clob_client.utilities import order_to_json

                tick_size = self._poly_client.get_tick_size(token_id)
                quantized_price = float(
                    Decimal(str(max_price)).quantize(
                        Decimal(str(tick_size)),
                        rounding=ROUND_HALF_UP,
                    )
                )
                buy_amount = round(contracts * quantized_price, 6)

                args = MarketOrderArgs(

                    token_id=token_id,

                    amount=buy_amount,

                    side="BUY",

                    price=quantized_price,

                    order_type=OrderType.FOK,

                )
                order = self._poly_client.create_market_order(args)
                body = order_to_json(order, self._poly_client.creds.api_key, OrderType.FOK, False)
                body_for_log = dict(body)
                body_for_log["owner"] = "<redacted>"
                exchange = get_contract_config(self._poly_client.signer.get_chain_id(), False).exchange
                logger.info(
                    "[live-debug] token_id=%s side=%s price=%s contracts=%s amount=%s maker=%s signer=%s "
                    "signature_type=%s chainId=%s exchange=%s order=%s payload=%s signature=%s",
                    token_id,
                    "BUY",
                    quantized_price,
                    contracts,
                    buy_amount,
                    order.dict().get("maker"),
                    order.dict().get("signer"),
                    order.dict().get("signatureType"),
                    self._poly_client.signer.get_chain_id(),
                    exchange,
                    order.dict(),
                    body_for_log,
                    order.signature,
                )


                # ── Residential proxy gate (CLOB geoblock workaround) ─
                # Swaps the module-level httpx.Client ONLY for post_order().
                # Reads, WS, and all upstream calls stay on direct connection.
                import py_clob_client.http_helpers.helpers as _ph
                import httpx as _httpx
                import time as _pt
                _proxy_url = os.getenv('CLOB_PROXY_URL', '')
                if _proxy_url:
                    _orig_client = _ph._http_client
                    try:
                        _proxied = _httpx.Client(
                            http2=True, proxy=_proxy_url, timeout=20.0
                        )
                    except TypeError:
                        # httpx < 0.24 uses proxies= keyword
                        _proxied = _httpx.Client(
                            http2=True, proxies=_proxy_url, timeout=20.0
                        )
                    _ph._http_client = _proxied
                    _pt0 = _pt.time()
                    try:
                        _res = self._poly_client.post_order(order)
                        logger.info(
                            f'[proxy] POST /order OK in {_pt.time()-_pt0:.2f}s'
                        )
                        return _res
                    except Exception as _pe:
                        _emsg = str(_pe)
                        _elapsed = _pt.time() - _pt0
                        if '403' in _emsg:
                            logger.error(
                                f'[proxy] 403 geoblock through proxy '
                                f'in {_elapsed:.2f}s — proxy may also be blocked: '
                                f'{_emsg[:150]}'
                            )
                        elif any(k in _emsg.lower()
                                 for k in ('proxy','connect','timeout','socks')):
                            logger.error(
                                f'[proxy] proxy connection failure '
                                f'in {_elapsed:.2f}s: {_emsg[:150]}'
                            )
                        else:
                            logger.error(
                                f'[proxy] upstream error in {_elapsed:.2f}s: '
                                f'{_emsg[:150]}'
                            )
                        raise
                    finally:
                        _ph._http_client = _orig_client
                        try:
                            _proxied.close()
                        except Exception:
                            pass
                else:
                    return self._poly_client.post_order(order)



            result = await loop.run_in_executor(None, _place)
            logger.info(f"[clob-result] raw={result}")
            # Fast-path: if result directly indicates a fill, skip activity polling
            fill_data = None
            if isinstance(result, dict):
                matched = result.get("matchedOrders") or result.get("fills") or []
                if matched:
                    logger.info(f"[clob-result] detected direct fill, skipping activity poll")
                    fill_data = await self._get_recent_live_trade_data(token_id, "BUY", submit_ts - 5)
            if fill_data is None:
                fill_data = await self._await_live_trade_data(token_id, "BUY", submit_ts)
            if fill_data is None:
                logger.error("Live fill reconciliation failed: no matching activity found")
                return None
            if float(fill_data.get("contracts", 0) or 0) <= 0 or float(fill_data.get("cost_usd", 0) or 0) <= 0:
                logger.error("Live fill reconciliation returned zero fill data; skipping position open")
                return None
            if "tx_hash" not in fill_data or not fill_data["tx_hash"]:
                tx_hash = result.get("transactionHash", "") if isinstance(result, dict) else str(result)
                fill_data["tx_hash"] = tx_hash or "live_ok"
            fill_data["ts_open"] = submit_ts
            return fill_data

        except Exception as e:

            logger.error(f"Live execution failed: {e}")

            # Post-exception activity rescue: the order may have filled on-chain
            # before the proxy dropped the connection. Wait briefly, then check.
            try:
                logger.warning("[rescue] _place() threw — checking activity API for fill (30s window)")
                await asyncio.sleep(5)
                rescue_data = await self._await_live_trade_data(token_id, "BUY", submit_ts, timeout_secs=30.0)
                if rescue_data and float(rescue_data.get("contracts", 0) or 0) > 0:
                    logger.info(f"[rescue] recovered fill from activity API: {rescue_data}")
                    rescue_data.setdefault("tx_hash", "rescue_ok")
                    rescue_data["ts_open"] = submit_ts
                    return rescue_data
                logger.warning("[rescue] no fill found in activity API after exception")
            except Exception as re:
                logger.error(f"[rescue] activity check failed: {re}")

            return None



    # ── Sizing ────────────────────────────────────────────────────────────────



    def _tax_reserve_usd(self) -> float:
        try:
            import json as _j
            with open('/root/tax_reserve.json') as _f:
                return float(_j.load(_f).get('reserve_usd', 0))
        except Exception:
            return 0.0

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



        dollars   = kelly * max(self.bankroll - self._tax_reserve_usd(), 1.0)

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



    async def _get_best_bid(self, token_id: str) -> float | None:
        try:
            r = await self._http.get(
                "https://clob.polymarket.com/book",
                params={"token_id": token_id},
            )
            if r.status_code != 200:
                return None
            bids = r.json().get("bids", [])
            if not bids:
                return None
            best = max(bids, key=lambda x: float(x.get("price", 0.0)))
            return float(best["price"])
        except Exception as e:
            logger.debug(f"_get_best_bid failed: {e}")
            return None

    async def _execute_live_sell(
        self, token_id: str, contracts: int, min_price: float
    ) -> tuple | None:
        """Send a SELL FOK order. Returns (fill_price, tx_hash) or None on failure."""
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        submit_ts = time.time()

        def _place_sell():
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            tick_size = self._poly_client.get_tick_size(token_id)
            quantized_price = float(
                Decimal(str(min_price)).quantize(
                    Decimal(str(tick_size)), rounding=ROUND_HALF_UP
                )
            )
            args = MarketOrderArgs(
                token_id=token_id,
                amount=float(contracts),
                side="SELL",
                price=quantized_price,
                order_type=OrderType.FOK,
            )
            order = self._poly_client.create_market_order(args)
            import py_clob_client.http_helpers.helpers as _ph
            import httpx as _httpx
            _proxy_url = os.getenv("CLOB_PROXY_URL", "")
            if _proxy_url:
                _orig = _ph._http_client
                try:
                    try:
                        _px = _httpx.Client(http2=True, proxy=_proxy_url, timeout=20.0)
                    except TypeError:
                        _px = _httpx.Client(http2=True, proxies=_proxy_url, timeout=20.0)
                    _ph._http_client = _px
                    return self._poly_client.post_order(order)
                finally:
                    _ph._http_client = _orig
                    try:
                        _px.close()
                    except Exception:
                        pass
            else:
                return self._poly_client.post_order(order)

        try:
            result = await loop.run_in_executor(None, _place_sell)
            logger.info(f"[sell-result] raw={result}")
            fill = await self._get_recent_live_trade_data(token_id, "SELL", submit_ts - 5)
            if fill:
                return (float(fill.get("price", min_price)), fill.get("tx_hash", "sell_ok"))
            return (min_price, "sell_ok")
        except Exception as exc:
            logger.error(f"_execute_live_sell failed: {exc}")
            return None

    async def close_position(
        self,
        pos: "Position",
        reason: str = "profit_lock",
        bid_price: float | None = None,
    ) -> bool:
        """
        Close an open position early by selling on the CLOB.
        Returns True if closed successfully.
        """
        if pos.status != "open":
            return False
        if bid_price is None:
            bid_price = await self._get_best_bid(pos.token_id)
        if bid_price is None or bid_price <= 0:
            logger.warning(f"close_position: no bid for {pos.id}")
            return False

        payout       = pos.contracts * bid_price
        realized_pnl = round(payout - pos.cost_usd, 4)
        now          = time.time()

        if self.paper_mode:
            pos.exit_price   = bid_price
            pos.realized_pnl = realized_pnl
            pos.ts_close     = now
            pos.status       = "won" if realized_pnl > 0 else "lost"
            pos.tx_hash      = reason
            self._log_position(pos, event="close")
            self._update_shared_bankroll(payout)
            logger.info(
                f"PROFIT LOCK [{reason}]: {pos.direction} {pos.symbol} "
                f"@{pos.entry_price:.3f}->{bid_price:.3f}  "
                f"pnl=${realized_pnl:+.2f}  payout=${payout:.2f}"
            )
            return True
        else:
            sold = await self._execute_live_sell(pos.token_id, pos.contracts, bid_price)
            if sold is None:
                logger.warning(f"close_position: live sell failed for {pos.id}")
                return False
            actual_price, tx = sold
            payout       = pos.contracts * actual_price
            realized_pnl = round(payout - pos.cost_usd, 4)
            pos.exit_price   = actual_price
            pos.realized_pnl = realized_pnl
            pos.ts_close     = now
            pos.status       = "won" if realized_pnl > 0 else "lost"
            pos.tx_hash      = tx
            self._log_position(pos, event="close")
            self._update_shared_bankroll(payout)
            logger.info(
                f"PROFIT LOCK LIVE [{reason}]: {pos.direction} {pos.symbol} "
                f"@{pos.entry_price:.3f}->{actual_price:.3f}  "
                f"pnl=${realized_pnl:+.2f}  payout=${payout:.2f}"
            )
            return True

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
