"""
arb_main.py — Pre-expiry drift arb for Polymarket 5-min crypto Up-or-Down markets.

Strategy: At T=-35s before each 5-min expiry, if Coinbase spot has drifted >0.05%
from the reference price (candle open at window start), buy the winning token on the
CLOB before market makers reprice at T≈-5s.

Empirical basis: 22/23 = 96% WR when |drift| > 0.05% (35-observation probe, 2026-04-17).
"""

import asyncio, time, json, math, logging, os, signal
from datetime import datetime, timezone
import httpx
from dotenv import load_dotenv
load_dotenv()

from poly_executor import PolyExecutor
from telegram_alerts import TelegramAlerts

# ── Config ──────────────────────────────────────────────────────────────────────
LOGS_DIR    = os.getenv('LOGS_DIR', 'logs_arb')
LOG_PATH    = f'{LOGS_DIR}/bot.log'
CLOB_PROXY  = os.getenv('CLOB_PROXY_URL', 'http://138.197.181.139:8083')
POLYGON_RPC = 'https://polygon-bor-rpc.publicnode.com'
GAMMA_URL   = 'https://gamma-api.polymarket.com/markets'
BOT_LABEL   = os.getenv('BOT_LABEL', 'ARB')
PAUSE_FLAG  = '/root/arb_paused'

ASSETS      = ['BTC', 'ETH', 'SOL', 'DOGE', 'XRP']
CB_PRODUCTS = {
    'BTC': 'BTC-USD', 'ETH': 'ETH-USD', 'SOL': 'SOL-USD',
    'DOGE': 'DOGE-USD', 'XRP': 'XRP-USD',
}
CHAINLINK_FEEDS = {
    'BTC':  '0xc907E116054Ad103354f2D350FD2514433D57F6f',
    'ETH':  '0xF9680D99D6C9589e2a93a78A04A279e509205945',
    'SOL':  '0x10C8264C0935b3B9870013e057f330Ff3e9C56dC',
    'DOGE': '0xbaf9327b6564454F4a3364C33eFeEf032b4b4444',
    'XRP':  '0x785ba89291f676b5386652eB12b30cF361020694',
}

MIN_DRIFT_PCT  = float(os.getenv('MIN_DRIFT_PCT', '0.0005'))   # 0.05%
MAX_WINNER_ASK = float(os.getenv('MAX_WINNER_ASK', '0.85'))
MIN_EV         = float(os.getenv('MIN_EV', '0.10'))
ENTRY_START_S  = int(os.getenv('ENTRY_START_SECS', '35'))
ENTRY_STOP_S   = int(os.getenv('ENTRY_STOP_SECS', '6'))
SCAN_INTERVAL  = int(os.getenv('SCAN_INTERVAL', '30'))
ARB_CONF       = float(os.getenv('ARB_CONF', '0.90'))          # fixed confidence for all arb entries
BANKROLL_USD   = float(os.getenv('BANKROLL_USD', '107'))
PAPER_TRADING  = os.getenv('PAPER_TRADING', '1') != '0'

os.makedirs(LOGS_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[logging.FileHandler(LOG_PATH, mode='a'), logging.StreamHandler()]
)
log = logging.getLogger('arb')


async def _send_shutdown(tg: TelegramAlerts):
    await tg.send('[ARB] \U0001f534 Bot stopped')
    asyncio.get_event_loop().stop()


# ── Price sources ───────────────────────────────────────────────────────────────
async def chainlink_price(client: httpx.AsyncClient, asset: str) -> float | None:
    feed = CHAINLINK_FEEDS.get(asset)
    if not feed:
        return None
    payload = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'eth_call',
        'params': [{'to': feed, 'data': '0xfeaf968c'}, 'latest'],
    }
    try:
        r = await client.post(POLYGON_RPC, json=payload, timeout=4)
        result = r.json().get('result', '')
        if not result or result == '0x':
            return None
        raw = bytes.fromhex(result[2:])
        answer = int.from_bytes(raw[32:64], 'big', signed=True)
        return answer / 1e8
    except:
        return None


async def coinbase_price(client: httpx.AsyncClient, asset: str) -> float | None:
    try:
        r = await client.get(
            f'https://api.exchange.coinbase.com/products/{CB_PRODUCTS[asset]}/ticker',
            timeout=4)
        return float(r.json()['price'])
    except:
        return None


async def curr_price(client: httpx.AsyncClient, asset: str) -> float | None:
    p = await chainlink_price(client, asset)
    if p:
        return p
    return await coinbase_price(client, asset)


async def cb_ref_price(client: httpx.AsyncClient, asset: str, window_start_ts: float) -> float | None:
    start = int(window_start_ts // 60) * 60
    try:
        r = await client.get(
            f'https://api.exchange.coinbase.com/products/{CB_PRODUCTS[asset]}/candles',
            params={'granularity': 60, 'start': start, 'end': start + 60},
            timeout=5)
        candles = r.json()
        if candles:
            return float(candles[0][3])  # open price
    except:
        pass
    return None


# ── Market discovery ─────────────────────────────────────────────────────────────
def _parse_market(m: dict, asset: str) -> dict | None:
    end_str = m.get('endDate') or m.get('endDateIso') or ''
    try:
        end_ts = datetime.fromisoformat(end_str.replace('Z', '+00:00')).timestamp()
    except:
        return None

    clob_ids = m.get('clobTokenIds') or '[]'
    if isinstance(clob_ids, str):
        try: clob_ids = json.loads(clob_ids)
        except: clob_ids = []

    outcomes = m.get('outcomes') or '[]'
    if isinstance(outcomes, str):
        try: outcomes = json.loads(outcomes)
        except: outcomes = []

    up_tok = dn_tok = None
    if len(clob_ids) >= 2:
        if outcomes and outcomes[0].lower() == 'up':
            up_tok, dn_tok = clob_ids[0], clob_ids[1]
        else:
            dn_tok, up_tok = clob_ids[0], clob_ids[1]

    tokens = m.get('tokens') or []
    if isinstance(tokens, str):
        try: tokens = json.loads(tokens)
        except: tokens = []
    for t in tokens:
        o = (t.get('outcome') or '').lower()
        tid = t.get('token_id') or t.get('tokenId') or ''
        if o == 'up': up_tok = tid
        elif o == 'down': dn_tok = tid

    if not up_tok or not dn_tok:
        return None

    return {
        'asset':        asset,
        'symbol':       f'{asset}-USD',
        'title':        m.get('question') or m.get('title') or '',
        'condition_id': m.get('conditionId') or m.get('condition_id') or '',
        'end_time':     end_ts,
        'secs_left':    end_ts - time.time(),
        'up_token':     up_tok,
        'dn_token':     dn_tok,
    }


async def find_markets(client: httpx.AsyncClient) -> list[dict]:
    now  = time.time()
    base = int(math.ceil(now / 300) * 300)
    found: dict[tuple, dict] = {}

    async def fetch(asset: str, slug: str):
        try:
            r = await client.get(GAMMA_URL, params={'slug': slug}, timeout=6)
            if r.status_code != 200:
                return
            ms = r.json()
            if isinstance(ms, dict):
                ms = ms.get('data', [])
            for m in ms:
                parsed = _parse_market(m, asset)
                if parsed and 10 < parsed['secs_left'] < 600:
                    key = (asset, round(parsed['end_time'] / 300) * 300)
                    found[key] = parsed
        except:
            pass

    tasks = [
        (a, f'{a.lower()}-updown-5m-{base + i * 300}')
        for a in ASSETS
        for i in range(-2, 4)
    ]
    await asyncio.gather(*[fetch(a, s) for a, s in tasks])
    return sorted(found.values(), key=lambda x: x['end_time'])


# ── CLOB ask ──────────────────────────────────────────────────────────────────
async def best_ask(client: httpx.AsyncClient, token_id: str) -> float | None:
    try:
        r = await client.get('https://clob.polymarket.com/book',
                             params={'token_id': token_id}, timeout=4)
        asks = r.json().get('asks', [])
        if asks:
            return float(min(asks, key=lambda x: float(x['price']))['price'])
    except:
        pass
    return None


# ── Per-market arb logic ───────────────────────────────────────────────────────
async def arb_one(mkt: dict, executor: PolyExecutor,
                  clob: httpx.AsyncClient, data: httpx.AsyncClient,
                  tg: 'TelegramAlerts'):
    asset     = mkt['asset']
    end_time  = mkt['end_time']
    up_tok    = mkt['up_token']
    dn_tok    = mkt['dn_token']
    label     = f"{asset}@{datetime.fromtimestamp(end_time, timezone.utc).strftime('%H:%M')}UTC"
    window_st = end_time - 300

    wait = end_time - ENTRY_START_S - time.time()
    if wait > 0:
        await asyncio.sleep(wait)

    ref = await cb_ref_price(data, asset, window_st)
    if not ref:
        log.warning(f'[{label}] no ref price — skipping')
        return

    log.info(f'[{label}] watching  ref={ref}  window T-{ENTRY_START_S}s to T-{ENTRY_STOP_S}s')

    entered = False
    while not entered and time.time() < end_time - ENTRY_STOP_S:
        t = time.time() - end_time  # negative = before expiry

        price, up_ask, dn_ask = await asyncio.gather(
            curr_price(data, asset),
            best_ask(clob, up_tok),
            best_ask(clob, dn_tok),
        )

        if not price:
            await asyncio.sleep(1.0)
            continue

        drift     = (price - ref) / ref
        direction = 'Up' if drift > 0 else 'Down'
        win_tok   = up_tok if direction == 'Up' else dn_tok
        win_ask   = up_ask if direction == 'Up' else dn_ask
        ev        = round(0.99 - win_ask, 4) if win_ask else None

        log.info(
            f'  [{label}] t={t:+6.1f}s  price={price}  drift={drift*100:+.4f}%'
            f'  dir={direction}  ask={win_ask}  ev={ev}'
        )

        if abs(drift) < MIN_DRIFT_PCT:
            await asyncio.sleep(1.0)
            continue
        if win_ask is None or win_ask >= MAX_WINNER_ASK:
            await asyncio.sleep(1.0)
            continue
        if ev is None or ev < MIN_EV:
            await asyncio.sleep(1.0)
            continue

        exec_mkt = {
            'symbol':        mkt['symbol'],
            'title':         mkt['title'],
            'condition_id':  mkt['condition_id'],
            'token_id':      win_tok,
            'up_token_id':   up_tok,
            'down_token_id': dn_tok,
            'end_time':      end_time,
            'secs_left':     end_time - time.time(),
        }

        log.info(
            f'  [{label}] >>> ENTER {direction}  drift={drift*100:+.4f}%  '
            f'ask={win_ask:.3f}  ev={ev:.3f}  conf={ARB_CONF}'
        )

        pos, skip = await executor.execute(
            exec_mkt, direction, ARB_CONF,
            source='arb', ask_price=win_ask,
        )
        entered = True  # one trade per market regardless of outcome

        if pos:
            log.info(
                f'  [{label}] ENTERED  contracts={pos.contracts}'
                f'  cost=${pos.cost_usd:.2f}'
            )
            secs_left = max(0, end_time - time.time())
            mins, secs = divmod(int(secs_left), 60)
            time_str = f'{mins}m{secs:02d}s' if mins else f'{secs}s'
            await tg.send(
                f'[ARB] {pos.symbol} {direction} | '
                f'drift={drift*100:+.4f}% ask={win_ask:.3f} ev={ev:.3f} '
                f'${pos.cost_usd:.2f} [{time_str}]'
            )
        else:
            log.info(f'  [{label}] skipped: {skip}')

        await asyncio.sleep(1.0)

    log.info(f'[{label}] entry window closed  entered={entered}')


# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    log.info(
        f'=== ARB BOT start  PAPER={os.getenv("PAPER_TRADING","1")}  '
        f'MIN_DRIFT={MIN_DRIFT_PCT*100:.3f}%  MIN_EV={MIN_EV}  '
        f'ENTRY={ENTRY_START_S}s–{ENTRY_STOP_S}s ==='
    )

    tg = TelegramAlerts()
    await tg.send(
        f'[ARB] \U0001f7e2 Bot started  LIVE={not PAPER_TRADING}  '
        f'MIN_DRIFT={MIN_DRIFT_PCT*100:.3f}%  MIN_EV={MIN_EV}  '
        f'ENTRY={ENTRY_START_S}s\u2013{ENTRY_STOP_S}s'
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(_send_shutdown(tg)))

    executor = PolyExecutor(bankroll_usd=BANKROLL_USD, paper_mode=PAPER_TRADING)

    session_wins = 0
    session_losses = 0
    session_pnl = 0.0

    async def settlement_loop():
        nonlocal session_wins, session_losses, session_pnl
        while True:
            try:
                prev = {pid: p.status for pid, p in executor._positions.items()}
                await executor.check_and_settle_expired()
                for pid, pos in executor._positions.items():
                    if prev.get(pid) == 'open' and pos.status in ('won', 'lost'):
                        won = pos.status == 'won'
                        pnl = pos.realized_pnl or 0.0
                        session_pnl += pnl
                        if won:
                            session_wins += 1
                        else:
                            session_losses += 1
                        total = session_wins + session_losses
                        wr = session_wins / total * 100 if total else 0
                        icon = '✅' if won else '❌'
                        entry_p = pos.entry_price or 0.0
                        exit_p = pos.exit_price or 0.0
                        msg = (
                            f'[ARB] {icon} {pos.symbol} {pos.direction} '
                            f'{"WON" if won else "LOST"} {pnl:+.2f}\n'
                            f'({entry_p:.3f}→{exit_p:.3f})\n'
                            f'Session: W{session_wins}/L{session_losses} '
                            f'{wr:.0f}% PnL={session_pnl:+.2f}'
                        )
                        await tg.send(msg)
            except Exception as e:
                log.warning(f'settlement error: {e}')
            await asyncio.sleep(60)

    asyncio.create_task(settlement_loop())

    transport = httpx.AsyncHTTPTransport(proxy=CLOB_PROXY)
    async with httpx.AsyncClient(transport=transport, timeout=10) as clob:
        async with httpx.AsyncClient(timeout=10) as data:
            scheduled: set[tuple] = set()

            while True:
                if os.path.exists(PAUSE_FLAG):
                    log.info('Paused (/root/arb_paused exists)')
                    await asyncio.sleep(30)
                    continue

                log.info('--- scanning ---')
                markets = await find_markets(data)
                now = time.time()

                for mkt in markets:
                    key = (mkt['asset'], round(mkt['end_time'] / 300) * 300)
                    if key in scheduled:
                        continue
                    # Need enough runway: at least ENTRY_START_S + 10s still remaining
                    if mkt['secs_left'] < ENTRY_START_S + 10:
                        continue
                    scheduled.add(key)
                    asyncio.create_task(arb_one(mkt, executor, clob, data, tg))
                    log.info(
                        f"  scheduled {mkt['asset']} "
                        f"{datetime.fromtimestamp(mkt['end_time'], timezone.utc).strftime('%H:%M')}UTC "
                        f"({mkt['secs_left']:.0f}s left)"
                    )

                # Expire old scheduled keys (>10 min past end_time)
                scheduled = {k for k in scheduled if k[1] > now - 600}

                await asyncio.sleep(SCAN_INTERVAL)


if __name__ == '__main__':
    asyncio.run(main())
