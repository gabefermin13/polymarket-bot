"""Tests for BiasTracker — rolling WR circuit breaker."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from bias_tracker import BiasTracker, PAUSE_THRESHOLD, RESUME_THRESHOLD, MIN_WINDOW, WINDOW_SIZE


# ── Core pause/resume logic ───────────────────────────────────────────────────

def test_not_paused_with_insufficient_data():
    bt = BiasTracker()
    bt.record("BTC", "Down", False)
    bt.record("BTC", "Down", False)
    # Only 2 trades, MIN_WINDOW=3, should not pause yet
    assert bt.is_paused("BTC", "Down") is False


def test_pauses_after_min_window_at_low_wr():
    bt = BiasTracker()
    # 0/3 wins = 0% WR < 40%
    for _ in range(3):
        bt.record("BTC", "Down", False)
    assert bt.is_paused("BTC", "Down") is True


def test_does_not_pause_above_threshold():
    bt = BiasTracker()
    # 2/3 wins = 67% WR > 40%
    bt.record("ETH", "Up", True)
    bt.record("ETH", "Up", True)
    bt.record("ETH", "Up", False)
    assert bt.is_paused("ETH", "Up") is False


def test_resumes_after_recovery():
    bt = BiasTracker()
    # Drive to paused (0% WR)
    for _ in range(3):
        bt.record("BTC", "Down", False)
    assert bt.is_paused("BTC", "Down") is True
    # Fill window with wins to recover above 50%
    for _ in range(WINDOW_SIZE):
        bt.record("BTC", "Down", True)
    assert bt.is_paused("BTC", "Down") is False


def test_hysteresis_prevents_immediate_resume():
    bt = BiasTracker()
    # Pause: 0% WR
    for _ in range(3):
        bt.record("SOL", "Up", False)
    assert bt.is_paused("SOL", "Up") is True
    # Add 1 win: window now has [F,F,F,T] = 25% — still below RESUME_THRESHOLD=50%
    bt.record("SOL", "Up", True)
    assert bt.is_paused("SOL", "Up") is True


def test_combos_are_independent():
    bt = BiasTracker()
    for _ in range(3):
        bt.record("BTC", "Down", False)
    assert bt.is_paused("BTC", "Down") is True
    assert bt.is_paused("BTC", "Up") is False
    assert bt.is_paused("ETH", "Down") is False


def test_window_rolls_off_old_trades():
    bt = BiasTracker()
    # Fill window with losses to pause
    for _ in range(WINDOW_SIZE):
        bt.record("DOGE", "Up", False)
    assert bt.is_paused("DOGE", "Up") is True
    # Now add WINDOW_SIZE wins — all old losses fall off
    for _ in range(WINDOW_SIZE):
        bt.record("DOGE", "Up", True)
    assert bt.is_paused("DOGE", "Up") is False


# ── Warmup ────────────────────────────────────────────────────────────────────

def test_warmup_with_nonexistent_file():
    bt = BiasTracker()
    bt.warmup("/nonexistent/positions.jsonl")   # should not raise
    assert bt.is_paused("BTC", "Up") is False


def test_warmup_loads_paused_combo(tmp_path):
    import json, time
    positions = tmp_path / "positions.jsonl"
    # Write 3 losing BTC Down closes
    ts = time.time()
    lines = ""
    for i in range(3):
        lines += json.dumps({
            "event": "close", "status": "lost",
            "symbol": "BTC-USD", "direction": "Down",
            "ts_close": ts + i
        }) + "\n"
    positions.write_text(lines)
    bt = BiasTracker()
    bt.warmup(str(positions))
    assert bt.is_paused("BTC", "Down") is True


def test_warmup_ignores_open_events(tmp_path):
    import json, time
    positions = tmp_path / "positions.jsonl"
    ts = time.time()
    lines = ""
    for i in range(5):
        lines += json.dumps({
            "event": "open", "status": "open",
            "symbol": "ETH-USD", "direction": "Up",
            "ts_close": ts + i
        }) + "\n"
    positions.write_text(lines)
    bt = BiasTracker()
    bt.warmup(str(positions))
    assert bt.is_paused("ETH", "Up") is False
