#!/usr/bin/env python3
import json, csv, os
from datetime import datetime, timezone

POSITIONS_FILES = [
    ('/root/kalshiedge_dbot_d2/logs_d2/positions.jsonl', 'D2'),
    ('/root/kalshiedge_whalebot/logs_w/positions.jsonl',  'W'),
]
OUTPUT = '/root/tax_record.csv'

FIELDS = [
    'trade_id', 'bot', 'date_opened_utc', 'date_closed_utc',
    'asset', 'direction', 'entry_price', 'exit_price',
    'contracts', 'cost_usd', 'gross_payout_usd', 'net_profit_usd',
    'tx_hash',
]

def load_existing_ids():
    if not os.path.exists(OUTPUT):
        return set()
    with open(OUTPUT) as f:
        reader = csv.DictReader(f)
        return {row['trade_id'] for row in reader}

def fmt_dt(ts):
    if not ts:
        return ''
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

def load_wins(path, bot_label):
    if not os.path.exists(path):
        return []
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
            if r.get('event') == 'open':
                records.setdefault(tid, {}).update(r)
            elif r.get('event') == 'close':
                records.setdefault(tid, {}).update(r)

    wins = []
    for tid, r in records.items():
        if r.get('status') != 'won':
            continue
        if r.get('paper'):
            continue
        if r.get('event') != 'close':
            continue
        contracts = float(r.get('contracts', 0))
        exit_price = float(r.get('exit_price', 0))
        cost = float(r.get('cost_usd', 0))
        pnl = float(r.get('realized_pnl', 0))
        gross = round(contracts * exit_price, 4)
        wins.append({
            'trade_id':         tid,
            'bot':              bot_label,
            'date_opened_utc':  fmt_dt(r.get('ts_open')),
            'date_closed_utc':  fmt_dt(r.get('ts_close')),
            'asset':            r.get('symbol', '').split('-')[0],
            'direction':        r.get('direction', ''),
            'entry_price':      r.get('entry_price', ''),
            'exit_price':       exit_price,
            'contracts':        contracts,
            'cost_usd':         cost,
            'gross_payout_usd': gross,
            'net_profit_usd':   round(pnl, 4),
            'tx_hash':          r.get('tx_hash', ''),
        })
    return wins

def main():
    existing = load_existing_ids()
    new_wins = []
    for path, label in POSITIONS_FILES:
        for w in load_wins(path, label):
            if w['trade_id'] not in existing:
                new_wins.append(w)

    if not new_wins:
        return

    write_header = not os.path.exists(OUTPUT)
    with open(OUTPUT, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if write_header:
            writer.writeheader()
        for w in sorted(new_wins, key=lambda x: x['date_closed_utc']):
            writer.writerow(w)
    print(f'Appended {len(new_wins)} new winning trade(s) to {OUTPUT}')

if __name__ == '__main__':
    main()
