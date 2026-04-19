"""
arb_probe_ticks.py — Tick-level trajectory logger for Polymarket 5-min crypto markets.

NO TRADES. Pure observation.

For every 5-min market, from T=-35s to expiry:
  - Logs price + asks every second (per-tick records)
  - Tracks drift_sustained_secs and ask direction per tick
  - At settlement, writes one market-level summary evaluating all predefined
    candidate rules simultaneously on the same trajectory
  - No backfitting: rule grid is hardcoded before any data is collected

Output: /tmp/arb_probe_ticks.jsonl
  Two record types per market:
    type="tick"    — one per second during watch window
    type="summary" — one per market at settlement

CANDIDATE RULE GRID (predefined, fixed):
  entry_windows_secs:    [35, 25, 20, 15]     seconds before expiry
  persistence_thresholds: [0, 3, 5, 8]        consecutive secs drift above MIN_DRIFT_PCT
  ask_bands:             [(0.75, 0.78),        win-side ask must be in this range
                          (0.75, 0.80)]

  All 4 × 4 × 2 = 32 combinations evaluated per market.

ANALYSIS USAGE:
  import pandas as pd, json

  records = [json.loads(l) for l in open('/tmp/arb_probe_ticks.jsonl')]
  summaries = [r for r in records if r['type'] == 'summary']
  ticks     = [r for r in records if r['type'] == 'tick']

  df = pd.DataFrame(summaries)

  # EV per hour by rule:
  rule = 'w20_p5_a0.75-0.78'
  entered = df[df[f'{rule}.entered']]
  print(entered[f'{rule}.ev'].mean(), len(entered))

  # WR by rule:
  print(entered['outcome'].mean())

  # Missed-trade cost: markets a tighter rule missed vs baseline
  baseline = 'w35_p0_a0.75-0.80'
  missed = df[df[f'{baseline}.entered'] & ~df[f'{rule}.entered']]
  print(missed['outcome'].mean())  # WR of trades the tighter rule would skip
"""

import asyncio, httpx, json, time, math, logging, os
from datetime import datetime, timezone
from itertools import product

# ── Config ────────────────────────────────────────────────────────────────────
LOG_PATH      = '/tmp/arb_probe_ticks.jsonl'
CLOB_PROXY    = os.getenv('CLOB_PROXY_URL', 'http://138.197.181.139:8083')
GAMMA_URL     = 'https://gamma-api.polymarket.com/markets'
MIN_DRIFT_PCT = 0.0005   # 0.05% — matches live bot
WATCH_START_S = 35
WATCH_STOP_S  = 2
SCAN_INTERVAL = 30
OUTCOME_WAIT  = 60
OUTCOME_TRIES = 20

# Confirmed active 5-min assets on Polymarket (verified 2026-04-19)
ASSETS = ['BTC', 'ETH', 'SOL', 'DOGE', 'XRP', 'BNB']

CB_PRODUCTS = {
    'BTC': 'BTC-USD', 'ETH': 'ETH-USD', 'SOL': 'SOL-USD',
    'DOGE': 'DOGE-USD', 'XRP': 'XRP-USD', 'BNB': 'BNB-USD',
}

# ── Predefined candidate rule grid (fixed before data collection) ─────────────
ENTRY_WINDOWS    = [35, 25, 20, 15]      # seconds before expiry to start evaluating
PERSISTENCE_THRS = [0, 3, 5, 8]         # consecutive secs drift must be above MIN_DRIFT_PCT
ASK_BANDS        = [(0.75, 0.78), (0.75, 0.80)]

def rule_key(window, persistence, ask_lo, ask_hi):
    return f'w{window}_p{persistence}_a{ask_lo}-{ask_hi}'

ALL_RULES = [
    (w, p, lo, hi)
    for w, p, (lo, hi) in product(ENTRY_WINDOWS, PERSISTENCE_THRS, ASK_BANDS)
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger('probe')

# ── Price sources ─────────────────────────────────────────────────────────────
async def coinbase_price(client: httpx.AsyncClient, asset: str) -> float | None:
    pair = CB_PRODUCTS.get(asset)
    if not pair:
        return None
    try:
        r = await client.get(
            f'https://api.exchange.coinbase.com/products/{pair}/ticker',
            timeout=4)
        return float(r.json()['price'])
    except:
        return None


async def cb_ref_price(client: httpx.AsyncClient, asset: str,
                       window_start_ts: float) -> float | None:
    pair = CB_PRODUCTS.get(asset)
    if not pair:
        return None
    start = int(window_start_ts // 60) * 60
    try:
        r = await client.get(
            f'https://api.exchange.coinbase.com/products/{pair}/candles',
            params={'granularity': 60, 'start': start, 'end': start + 60},
            timeout=5)
        candles = r.json()
        if candles:
            return float(candles[0][3])  # open
    except:
        pass
    return None


# ── CLOB ──────────────────────────────────────────────────────────────────────
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


async def fetch_outcome(client: httpx.AsyncClient, up_token: str) -> int | None:
    try:
        r = await client.get('https://clob.polymarket.com/last-trade-price',
                             params={'token_id': up_token}, timeout=5)
        p = float(r.json().get('price', 0.5))
        if p >= 0.95:
            return 1
        if p <= 0.05:
            return 0
    except:
        pass
    return None


# ── Market discovery ──────────────────────────────────────────────────────────
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


# ── Ask direction helper ──────────────────────────────────────────────────────
def ask_direction(prev: float | None, curr: float | None) -> str:
    if prev is None or curr is None:
        return 'unknown'
    if curr > prev + 0.005:
        return 'rising'
    if curr < prev - 0.005:
        return 'falling'
    return 'flat'


# ── Candidate rule evaluation ─────────────────────────────────────────────────
def evaluate_rules(ticks: list[dict]) -> dict:
    """
    For each predefined rule, determine:
      - whether the rule would have entered this market
      - at what ask price (first qualifying tick)
      - EV of that entry (0.99 - ask)
      - realised P&L at outcome (filled in later with outcome)

    Returns flat dict keyed by rule_key(w, p, lo, hi).
    """
    results = {}
    for w, p, lo, hi in ALL_RULES:
        key     = rule_key(w, p, lo, hi)
        entered = False
        entry_ask = None
        entry_secs_left = None

        for tick in ticks:
            sl  = tick['secs_left']
            ask = tick['win_ask']
            sus = tick['drift_sustained_secs']

            if sl > w:
                continue                          # outside this rule's window
            if sl < WATCH_STOP_S:
                break                             # past safe entry zone
            if ask is None:
                continue
            if not (lo <= ask <= hi):
                continue
            if sus < p:
                continue
            if abs(tick['drift_pct']) < MIN_DRIFT_PCT * 100:
                continue

            entered         = True
            entry_ask       = ask
            entry_secs_left = sl
            break

        ev = round(0.99 - entry_ask, 4) if entered else None
        results[key] = {
            f'{key}.entered':         entered,
            f'{key}.entry_ask':       entry_ask,
            f'{key}.entry_secs_left': entry_secs_left,
            f'{key}.ev':              ev,
        }
    return results


# ── Trajectory snapshots ──────────────────────────────────────────────────────
SNAPSHOT_TIMES = [35, 30, 25, 20, 15, 10, 5]   # secs_left targets

def trajectory_snapshots(ticks: list[dict]) -> dict:
    """
    For each snapshot time, find the tick closest to that secs_left value
    and record key fields. Gives a fixed-point view of how the market evolved.
    """
    snaps = {}
    for target in SNAPSHOT_TIMES:
        closest = min(
            (t for t in ticks if abs(t['secs_left'] - target) < 2),
            key=lambda t: abs(t['secs_left'] - target),
            default=None,
        )
        if closest:
            snaps[f'snap_t{target}_drift']     = closest['drift_pct']
            snaps[f'snap_t{target}_win_ask']   = closest['win_ask']
            snaps[f'snap_t{target}_sustained'] = closest['drift_sustained_secs']
            snaps[f'snap_t{target}_direction'] = closest['direction']
        else:
            snaps[f'snap_t{target}_drift']     = None
            snaps[f'snap_t{target}_win_ask']   = None
            snaps[f'snap_t{target}_sustained'] = None
            snaps[f'snap_t{target}_direction'] = None
    return snaps


# ── Per-market probe ───────────────────────────────────────────────────────────
async def probe_market(mkt: dict, clob: httpx.AsyncClient, data: httpx.AsyncClient):
    asset     = mkt['asset']
    end_time  = mkt['end_time']
    up_tok    = mkt['up_token']
    dn_tok    = mkt['dn_token']
    cid       = mkt['condition_id']
    window_st = end_time - 300
    label     = f"{asset}@{datetime.fromtimestamp(end_time, timezone.utc).strftime('%H:%M')}UTC"

    wait = end_time - WATCH_START_S - time.time()
    if wait > 0:
        await asyncio.sleep(wait)

    ref = await cb_ref_price(data, asset, window_st)
    if not ref:
        log.warning(f'[{label}] no ref price — skipping')
        return

    log.info(f'[{label}] watching  ref={ref}')

    ticks            = []
    drift_sustained  = 0
    prev_win_ask     = None
    drift_ever_reversed = False

    while time.time() < end_time - WATCH_STOP_S:
        ts        = time.time()
        secs_left = end_time - ts

        price, up_ask_v, dn_ask_v = await asyncio.gather(
            coinbase_price(data, asset),
            best_ask(clob, up_tok),
            best_ask(clob, dn_tok),
        )

        if price is None:
            await asyncio.sleep(1.0)
            continue

        drift_pct = (price - ref) / ref * 100
        direction = 'Up' if drift_pct > 0 else 'Down'
        win_ask   = up_ask_v if direction == 'Up' else dn_ask_v

        # Drift persistence counter
        if abs(drift_pct) >= MIN_DRIFT_PCT * 100:
            drift_sustained += 1
        else:
            if drift_sustained > 0:
                drift_ever_reversed = True
            drift_sustained = 0

        # Ask direction vs previous tick
        ask_dir = ask_direction(prev_win_ask, win_ask)
        prev_win_ask = win_ask

        tick = {
            'type':                 'tick',
            'condition_id':         cid,
            'asset':                asset,
            'end_time':             end_time,
            'secs_left':            round(secs_left, 2),
            'ts':                   round(ts, 3),
            'ref':                  ref,
            'price':                price,
            'drift_pct':            round(drift_pct, 5),
            'direction':            direction,
            'up_ask':               up_ask_v,
            'dn_ask':               dn_ask_v,
            'win_ask':              win_ask,
            'drift_sustained_secs': drift_sustained,
            'ask_direction':        ask_dir,
            'outcome':              None,   # backfilled at settlement
        }
        ticks.append(tick)
        await asyncio.sleep(1.0)

    if not ticks:
        log.info(f'[{label}] no ticks recorded')
        return

    log.info(f'[{label}] {len(ticks)} ticks — waiting for outcome')

    # Settle
    await asyncio.sleep(OUTCOME_WAIT)
    outcome = None
    for _ in range(OUTCOME_TRIES):
        outcome = await fetch_outcome(data, up_tok)
        if outcome is not None:
            break
        await asyncio.sleep(10)

    if outcome is None:
        log.warning(f'[{label}] outcome unknown after {OUTCOME_TRIES} tries')

    # Backfill outcome into ticks and write tick records
    with open(LOG_PATH, 'a') as f:
        for tick in ticks:
            tick['outcome'] = outcome
            f.write(json.dumps(tick) + '\n')

    # ── Market-level summary ──────────────────────────────────────────────────
    max_drift   = max((abs(t['drift_pct']) for t in ticks), default=0)
    drift_at_close = ticks[-1]['drift_pct'] if ticks else None

    rule_evals = evaluate_rules(ticks)
    snapshots  = trajectory_snapshots(ticks)

    summary = {
        'type':                 'summary',
        'condition_id':         cid,
        'asset':                asset,
        'end_time':             end_time,
        'outcome':              outcome,
        'n_ticks':              len(ticks),
        'max_drift_pct':        round(max_drift, 5),
        'drift_at_close':       round(drift_at_close, 5) if drift_at_close else None,
        'drift_ever_reversed':  drift_ever_reversed,
        'max_drift_sustained':  max((t['drift_sustained_secs'] for t in ticks), default=0),
    }
    summary.update(snapshots)

    # Flatten rule evaluations and add realised P&L
    for w, p, lo, hi in ALL_RULES:
        key = rule_key(w, p, lo, hi)
        rule_data = rule_evals[key]
        summary.update(rule_data)

        # Realised P&L if entered
        if rule_data[f'{key}.entered'] and outcome is not None:
            ask = rule_data[f'{key}.entry_ask']
            if ask is not None:
                pnl = (1.0 - ask) if outcome == 1 else -ask
                summary[f'{key}.pnl'] = round(pnl, 4)
            else:
                summary[f'{key}.pnl'] = None
        else:
            summary[f'{key}.pnl'] = None

    with open(LOG_PATH, 'a') as f:
        f.write(json.dumps(summary) + '\n')

    won_str = {1: 'Up WON', 0: 'Down WON', None: 'UNKNOWN'}[outcome]
    entered_rules = sum(
        1 for w, p, lo, hi in ALL_RULES
        if summary.get(f'{rule_key(w,p,lo,hi)}.entered')
    )
    log.info(f'[{label}] {won_str}  ticks={len(ticks)}  rules_entered={entered_rules}/32')


# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    log.info('=== ARB PROBE started ===')
    log.info(f'Output: {LOG_PATH}')
    log.info(f'Watch window: T=-{WATCH_START_S}s to T=-{WATCH_STOP_S}s')
    log.info(f'Assets: {ASSETS}')
    log.info(f'Rule grid: {len(ALL_RULES)} rules '
             f'({len(ENTRY_WINDOWS)} windows × '
             f'{len(PERSISTENCE_THRS)} persistence × '
             f'{len(ASK_BANDS)} ask bands)')
    log.info('Rules: ' + ', '.join(
        rule_key(w, p, lo, hi) for w, p, (lo, hi) in
        product(ENTRY_WINDOWS, PERSISTENCE_THRS, ASK_BANDS)
    ))

    transport = httpx.AsyncHTTPTransport(proxy=CLOB_PROXY)
    async with httpx.AsyncClient(transport=transport, timeout=10) as clob:
        async with httpx.AsyncClient(timeout=10) as data:
            scheduled: set[tuple] = set()

            while True:
                markets = await find_markets(data)
                now = time.time()

                new = 0
                for mkt in markets:
                    key = (mkt['asset'], round(mkt['end_time'] / 300) * 300)
                    if key in scheduled:
                        continue
                    if mkt['secs_left'] < WATCH_START_S + 5:
                        continue
                    scheduled.add(key)
                    asyncio.create_task(probe_market(mkt, clob, data))
                    new += 1

                active_assets = {k[0] for k in scheduled if k[1] > now - 600}
                log.info(
                    f'scan: {len(markets)} markets  '
                    f'{new} new  '
                    f'assets: {sorted(active_assets)}'
                )

                scheduled = {k for k in scheduled if k[1] > now - 600}
                await asyncio.sleep(SCAN_INTERVAL)


if __name__ == '__main__':
    asyncio.run(main())
