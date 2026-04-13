import json, time, fcntl, httpx

now = time.time()

BANKROLL_FILE = "/root/shared_bankroll.json"
LOCK_FILE     = "/root/shared_bankroll.json.lock"

def settle_position(pos, exit_price, bot_path):
    payout = pos["contracts"] * exit_price
    cost   = pos["cost_usd"]
    pnl    = payout - cost

    close_event = dict(pos)
    close_event.update({
        "event": "close",
        "status": "won" if exit_price >= 0.85 else "lost",
        "exit_price": exit_price,
        "realized_pnl": round(pnl, 4),
        "ts_close": now,
        "force_settled": True
    })

    with open(f"{bot_path}/positions.jsonl", "a") as f:
        f.write(json.dumps(close_event) + "\n")

    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            with open(BANKROLL_FILE) as bf:
                data = json.load(bf)
            data["balance"] = round(data["balance"] + payout, 4)
            with open(BANKROLL_FILE, "w") as bf:
                json.dump(data, bf)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

    return pnl

total_settled = 0
total_pnl = 0.0

for bot_label, bot_path in [("D2", "/root/kalshiedge_dbot_d2/logs_d2"),
                              ("W",  "/root/kalshiedge_whalebot/logs_w")]:
    positions = {}
    try:
        with open(f"{bot_path}/positions.jsonl") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    p = json.loads(line)
                    if p.get("id"):
                        positions[p["id"]] = p
                except Exception:
                    pass
    except FileNotFoundError:
        continue

    stuck = [(pid, p) for pid, p in positions.items()
             if p.get("event") == "open" and p.get("status") == "open"
             and p.get("end_time", 0) < now]

    print(f"[{bot_label}] {len(stuck)} stuck positions")

    with httpx.Client(timeout=10) as client:
        for pid, p in stuck:
            token_id = p["token_id"]
            try:
                r = client.get(
                    "https://clob.polymarket.com/last-trade-price",
                    params={"token_id": token_id}
                )
                r.raise_for_status()
                price = float(r.json().get("price", 0))
            except Exception as e:
                print(f"  SKIP {p['symbol']} {p['direction']} -- fetch failed: {e}")
                continue

            if price >= 0.85:
                pnl = settle_position(p, exit_price=price, bot_path=bot_path)
                print(f"  WON  {p['symbol']} {p['direction']} | price={price:.3f} pnl={pnl:+.2f}")
                total_settled += 1
                total_pnl += pnl
            elif price <= 0.15:
                pnl = settle_position(p, exit_price=price, bot_path=bot_path)
                print(f"  LOST {p['symbol']} {p['direction']} | price={price:.3f} pnl={pnl:+.2f}")
                total_settled += 1
                total_pnl += pnl
            else:
                print(f"  PENDING {p['symbol']} {p['direction']} | price={price:.3f} -- market not resolved yet")

print(f"\nTotal settled: {total_settled} | Total PnL: {total_pnl:+.2f}")
