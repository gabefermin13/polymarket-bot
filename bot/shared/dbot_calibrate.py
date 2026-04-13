#!/usr/bin/env python3
"""
dbot_calibrate.py — D-bot signal confidence calibration

Reads signals.jsonl (executed trades) and positions.jsonl (outcomes) from all
D-bots, tracks per-signal win rates, and writes signal_calibration.json.

Run via cron every 30 minutes:
    */30 * * * *  python3 /root/dbot_calibrate.py >> /tmp/dbot_calibrate.log 2>&1

Output: /root/signal_calibration.json  (read by all D-bots via hot-reload)
"""

import json
import os
import time
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────

DBOT_DIRS = [f"/root/kalshiedge_dbot_d{d}" for d in range(1, 8)]
OUTPUT_FILE = "/root/signal_calibration.json"

# Only use trades from after S6/S7 fix + timing gate deployment (2026-04-07 22:28 UTC).
# Trades before this used broken signals and no timing gates — their WRs are not
# representative and would penalise calibration weights unfairly.
CUTOFF_TS = 1775600880.0  # unix seconds; set to 0.0 to disable

# Prior win rates per signal (midpoint of conf ranges from direction_signals.py)
SIGNAL_PRIORS = {
    "s1": 0.65,   # price action      0.55-0.75
    "s2": 0.665,  # order flow        0.55-0.78
    "s3": 0.645,  # market structure  0.57-0.72
    "s4": 0.71,   # liquidations      0.60-0.82
    "s5": 0.665,  # cross-asset       0.60-0.73
    "s6": 0.63,   # poly flow         0.56-0.70
    "s7": 0.85,   # whale consensus   0.75-0.95
}

# Minimum trades before we trust observed WR over prior
FULL_TRUST_N  = 20   # n >= 20: use obs_wr fully
BLEND_MIN_N   = 5    # n >= 5: blend obs_wr with prior
PRIOR_WEIGHT  = 10   # pseudo-count for prior in Bayesian blend


def _bayesian_blend(obs_wr: float, n: int, prior: float) -> float:
    if n >= FULL_TRUST_N:
        return obs_wr
    if n >= BLEND_MIN_N:
        return (obs_wr * n + prior * PRIOR_WEIGHT) / (n + PRIOR_WEIGHT)
    return prior


def _read_jsonl(path: str) -> list[dict]:
    records = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
    except FileNotFoundError:
        pass
    return records


def main():
    t0 = time.time()

    # ── Load all executed signals and closed positions ────────────────────────

    # executed signals: key = (condition_id, direction) → signal_details dict
    executed: dict[tuple, dict] = {}
    for d in range(1, 8):
        bot_dir = f"/root/kalshiedge_dbot_d{d}"
        logs_dir = f"{bot_dir}/logs_d{d}"
        for rec in _read_jsonl(f"{logs_dir}/signals.jsonl"):
            if rec.get("executed") and rec.get("signals"):
                if CUTOFF_TS > 0 and rec.get("ts", 0) / 1000 < CUTOFF_TS:
                    continue
                key = (rec.get("condition_id", ""), rec.get("direction", ""))
                if key[0]:
                    executed[key] = rec["signals"]

    # closed positions: key = (condition_id, direction) → status (won/lost)
    outcomes: dict[tuple, str] = {}
    for d in range(1, 8):
        bot_dir = f"/root/kalshiedge_dbot_d{d}"
        logs_dir = f"{bot_dir}/logs_d{d}"
        for rec in _read_jsonl(f"{logs_dir}/positions.jsonl"):
            if rec.get("event") == "close" and rec.get("status") in ("won", "lost"):
                if CUTOFF_TS > 0 and rec.get("ts_open", 0) < CUTOFF_TS:
                    continue
                key = (rec.get("condition_id", ""), rec.get("direction", ""))
                if key[0]:
                    outcomes[key] = rec["status"]

    # ── Match signals to outcomes ─────────────────────────────────────────────

    # Per-signal stats: {signal_name: {"n": 0, "wins": 0}}
    sig_stats: dict[str, dict] = {s: {"n": 0, "wins": 0} for s in SIGNAL_PRIORS}

    # Per-signal per-asset stats
    asset_sig_stats: dict[str, dict[str, dict]] = defaultdict(
        lambda: {s: {"n": 0, "wins": 0} for s in SIGNAL_PRIORS}
    )

    matched = 0
    for key, signal_details in executed.items():
        condition_id, trade_direction = key
        outcome = outcomes.get(key)
        if outcome is None:
            continue  # trade still open or not yet settled

        matched += 1
        won = outcome == "won"

        # Determine which signals agreed with the trade direction
        trade_dir_upper = trade_direction.upper()  # "UP" or "DOWN"
        for sig_name, sig_val in signal_details.items():
            sig_name = sig_name.lower()
            if sig_name not in SIGNAL_PRIORS:
                continue
            sig_dir = sig_val.get("direction", "").upper() if isinstance(sig_val, dict) else ""
            if sig_dir == trade_dir_upper:
                sig_stats[sig_name]["n"] += 1
                if won:
                    sig_stats[sig_name]["wins"] += 1

                # Also track per asset (need to look up asset from executed record)
                # We stored the full rec, but only signal_details — use condition lookup
                # For now, track overall only (asset breakdown needs full rec storage)

    # ── Compute calibrated weights ────────────────────────────────────────────

    calibrated: dict[str, dict] = {}
    for sig_name, prior in SIGNAL_PRIORS.items():
        st = sig_stats[sig_name]
        n    = st["n"]
        wins = st["wins"]
        obs_wr = wins / n if n > 0 else prior
        blended = _bayesian_blend(obs_wr, n, prior)
        weight  = round(blended / prior, 4) if prior > 0 else 1.0
        calibrated[sig_name] = {
            "n":         n,
            "wins":      wins,
            "obs_wr":    round(obs_wr, 4)  if n > 0 else None,
            "prior":     prior,
            "blended":   round(blended, 4),
            "weight":    weight,
        }

    # ── Write output ─────────────────────────────────────────────────────────

    output = {
        "generated_at":   time.time(),
        "generated_at_ts": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "n_executed":     len(executed),
        "n_outcomes":     len(outcomes),
        "n_matched":      matched,
        "signals":        calibrated,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] dbot_calibrate: "
          f"{matched} matched trades, wrote {OUTPUT_FILE} in {elapsed:.2f}s")
    for sig, data in calibrated.items():
        n = data["n"]
        if n > 0:
            print(f"  {sig}: n={n} obs_wr={data['obs_wr']:.3f} "
                  f"blended={data['blended']:.3f} weight={data['weight']:.3f} "
                  f"(prior={data['prior']:.3f})")
        else:
            print(f"  {sig}: n=0 — using prior {data['prior']:.3f}")


if __name__ == "__main__":
    main()
