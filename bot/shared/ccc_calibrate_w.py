#!/usr/bin/env python3
"""
ccc_calibrate.py — Closed-position Confidence Calibration (updated)

Reads all C-bot + Whalebot positions.jsonl, computes per-wallet observed win rates,
blends with on-chain priors using Bayesian formula, and writes wallet_calibration.json
to all bot directories.

Bayesian blend:
    n >= 10:  conf = obs_wr                             (full trust observed)
    n >=  5:  conf = (obs_wr*n + prior*10) / (n+10)    (blend with prior)
    n <   5:  conf = prior                              (insufficient data)

Run:
    python3 /root/kalshiedge_whalewallet/ccc_calibrate.py
"""
import json, re, os, shutil
from collections import defaultdict

LOGS = [
    '/root/logs_c1/positions.jsonl',
    '/root/kalshiedge_whalewallet/logs/positions.jsonl',
    '/root/logs_c3/positions.jsonl',
    '/root/logs_c4/positions.jsonl',
    '/root/kalshiedge_whalebot/logs_w/positions.jsonl',  # whalebot data too
]

MIN_OBS_FOR_FULL  = 10
MIN_OBS_FOR_BLEND = 5

# Canonical output (all bot dirs copy from here)
OUT_PATH = '/root/kalshiedge_whalewallet/wallet_calibration.json'

# ── Collect positions ─────────────────────────────────────────────────────────

wallets = defaultdict(lambda: {'wins': 0, 'losses': 0})

for path in LOGS:
    events = {}
    try:
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                pid = e.get('id')
                if pid:
                    events[pid] = e
            except Exception:
                pass
    except Exception as ex:
        print(f'  Warning: could not read {path}: {ex}')
        continue

    for t in events.values():
        pnl = t.get('realized_pnl')
        if pnl is None:
            continue
        src = t.get('source', '')
        if not src.startswith('whale:'):
            continue
        name = src.replace('whale:', '').replace(':15m', '')
        if float(pnl) > 0:
            wallets[name]['wins'] += 1
        else:
            wallets[name]['losses'] += 1

# ── Compute calibrated confidence ─────────────────────────────────────────────

def claimed_wr(name: str) -> float:
    """Extract claimed WR from scan wallet name like scan_99wr_211n → 0.99."""
    m = re.search(r'_(\d+)wr', name)
    return int(m.group(1)) / 100.0 if m else 0.55

calibration = {}
print(f"{'Wallet':<32} {'N':>5} {'ObsWR':>7} {'Prior':>6} {'CalibConf':>10}")
print('-' * 65)

for name, d in sorted(wallets.items(), key=lambda x: -(x[1]['wins'] + x[1]['losses'])):
    n   = d['wins'] + d['losses']
    obs = d['wins'] / n if n > 0 else 0.0
    prior = claimed_wr(name)

    if n >= MIN_OBS_FOR_FULL:
        conf = obs
    elif n >= MIN_OBS_FOR_BLEND:
        conf = (obs * n + prior * MIN_OBS_FOR_FULL) / (n + MIN_OBS_FOR_FULL)
    else:
        conf = prior

    conf = round(min(conf, 0.95), 4)
    calibration[name] = conf
    print(f'{name:<32} {n:>5} {obs*100:>6.1f}% {prior*100:>5.0f}% {conf:>10.4f}')

# ── Write output ──────────────────────────────────────────────────────────────

with open(OUT_PATH, 'w') as f:
    json.dump(calibration, f, indent=2)

print(f'\nWrote {len(calibration)} wallet confidence values to {OUT_PATH}')

# Copy to all bot dirs
OTHER_DIRS = [
    '/root/kalshiedge_whalewallet_c1',
    '/root/kalshiedge_whalewallet_c3',
    '/root/kalshiedge_whalewallet_c4',
    '/root/kalshiedge_whalebot',           # whalebot dir
]

for d in OTHER_DIRS:
    if not os.path.isdir(d):
        print(f'  Skip (not found): {d}')
        continue
    dst = os.path.join(d, 'wallet_calibration.json')
    shutil.copy(OUT_PATH, dst)
    print(f'  Copied to {dst}')
