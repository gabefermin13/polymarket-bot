#!/usr/bin/env python3
"""
tax_reserve_update.py
Runs every 30 min. Reads new winning trades, computes marginal tax reserve,
updates /root/tax_reserve.json.

Bracket table is based on:
  base_income = $6,000  |  standard_deduction = $15,000
  => first $9,000 of bot profits are below taxable threshold
"""
import json, os
from datetime import datetime, timezone

POSITIONS_FILES = [
    ('/root/kalshiedge_dbot_d2/logs_d2/positions.jsonl', 'D2'),
    ('/root/kalshiedge_whalebot/logs_w/positions.jsonl',  'W'),
]
RESERVE_PATH = '/root/tax_reserve.json'

# (bot_profit_upper_limit, reserve_rate)
# Accounts for $6K base income and $15K standard deduction ($9K free zone)
BRACKETS = [
    (9_000,       0.05),   # 0% federal; 5% covers state buffer
    (20_925,      0.13),   # 10% federal + state
    (57_475,      0.15),   # 12% federal + state
    (112_350,     0.25),   # 22% federal + state
    (206_300,     0.28),   # 24% federal + state
    (float('inf'), 0.35),  # 32%+ federal + state
]

def marginal_reserve(prev_ytd: float, new_profit: float) -> float:
    reserve = 0.0
    remaining = new_profit
    cursor = prev_ytd
    for upper, rate in BRACKETS:
        if cursor >= upper:
            continue
        chunk = min(remaining, upper - cursor)
        reserve += chunk * rate
        remaining -= chunk
        cursor += chunk
        if remaining <= 0:
            break
    return round(reserve, 4)

def load_reserve() -> dict:
    if os.path.exists(RESERVE_PATH):
        with open(RESERVE_PATH) as f:
            return json.load(f)
    return {'reserve_usd': 0.0, 'ytd_bot_profits': 0.0, 'entries': []}

def load_wins():
    seen = set()
    wins = []
    for path, bot in POSITIONS_FILES:
        if not os.path.exists(path):
            continue
        records = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except:
                    continue
                tid = r.get('id')
                if not tid:
                    continue
                records.setdefault(tid, {}).update(r)
        for tid, r in records.items():
            if r.get('status') != 'won':
                continue
            if r.get('paper'):
                continue
            if r.get('event') != 'close':
                continue
            pnl = float(r.get('realized_pnl') or 0)
            if pnl <= 0:
                continue
            wins.append({'trade_id': tid, 'net_profit': pnl, 'bot': bot,
                         'ts_close': r.get('ts_close', 0)})
    return wins

def main():
    data = load_reserve()
    existing_ids = {e['trade_id'] for e in data.get('entries', [])}
    wins = [w for w in load_wins() if w['trade_id'] not in existing_ids]
    if not wins:
        return
    wins.sort(key=lambda x: x['ts_close'] or 0)
    for w in wins:
        rate_applied = None
        cursor = data['ytd_bot_profits']
        for upper, rate in BRACKETS:
            if cursor < upper:
                rate_applied = rate
                break
        reserved = marginal_reserve(data['ytd_bot_profits'], w['net_profit'])
        data['ytd_bot_profits'] = round(data['ytd_bot_profits'] + w['net_profit'], 4)
        data['reserve_usd']     = round(data['reserve_usd'] + reserved, 4)
        data.setdefault('entries', []).append({
            'trade_id':   w['trade_id'],
            'bot':        w['bot'],
            'net_profit': w['net_profit'],
            'rate':       rate_applied,
            'reserved':   reserved,
            'ytd_after':  data['ytd_bot_profits'],
            'ts':         datetime.now(timezone.utc).isoformat(),
        })
        print(f"  {w['trade_id']}: profit=${w['net_profit']:.2f} rate={rate_applied:.0%} reserved=${reserved:.2f} | YTD=${data['ytd_bot_profits']:.2f} total_reserve=${data['reserve_usd']:.2f}")
    data['last_updated'] = datetime.now(timezone.utc).isoformat()
    with open(RESERVE_PATH, 'w') as f:
        json.dump(data, f, indent=2)

if __name__ == '__main__':
    main()
