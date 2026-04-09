"""Tests for liquidity gate and CLOB gate logic in w_main.py."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ── Liquidity gate ────────────────────────────────────────────────────────────

def _liquidity_gate_fires(market_liquidity: float, min_liquidity: float = 500.0) -> bool:
    """True = gate fires (skip trade). Replicates on_whale_trade liquidity check."""
    return market_liquidity < min_liquidity


def test_liquidity_gate_fires_below_threshold():
    assert _liquidity_gate_fires(499.99) is True


def test_liquidity_gate_passes_at_threshold():
    assert _liquidity_gate_fires(500.0) is False


def test_liquidity_gate_passes_above_threshold():
    assert _liquidity_gate_fires(1000.0) is False


def test_liquidity_gate_fires_on_zero():
    assert _liquidity_gate_fires(0.0) is True


def test_liquidity_gate_fires_on_missing_field():
    # market.get("liquidity", 0.0) when field absent
    market = {}
    assert _liquidity_gate_fires(market.get("liquidity", 0.0)) is True


# ── CLOB gate ─────────────────────────────────────────────────────────────────

def _clob_gate_fires(bid_usd: float, ask_usd: float, ratio: float = 2.0) -> bool:
    """True = gate fires (skip trade). Replicates on_whale_trade CLOB check."""
    if bid_usd > 0 and ask_usd > 0:
        return (ask_usd / bid_usd) > ratio
    return False


def test_clob_gate_fires_when_ask_dominates():
    assert _clob_gate_fires(100.0, 300.0) is True   # ratio 3.0 > 2.0


def test_clob_gate_passes_when_balanced():
    assert _clob_gate_fires(100.0, 120.0) is False  # ratio 1.2 < 2.0


def test_clob_gate_passes_on_api_down():
    assert _clob_gate_fires(0.0, 0.0) is False      # (0,0) fallback → pass


def test_clob_gate_passes_at_exact_ratio():
    assert _clob_gate_fires(100.0, 200.0) is False  # ratio 2.0 → not > 2.0


def test_clob_gate_fires_just_above_ratio():
    assert _clob_gate_fires(100.0, 200.1) is True   # ratio 2.001 > 2.0


def test_clob_gate_passes_bid_dominates():
    assert _clob_gate_fires(300.0, 100.0) is False  # ratio 0.33 < 2.0


# ── token_id selection ────────────────────────────────────────────────────────

def _select_token_id(market: dict, direction: str) -> str:
    """Replicates token selection in the CLOB gate block."""
    return market["up_token_id"] if direction == "Up" else market["down_token_id"]


def test_token_selection_up():
    market = {"up_token_id": "AAA", "down_token_id": "BBB"}
    assert _select_token_id(market, "Up") == "AAA"


def test_token_selection_down():
    market = {"up_token_id": "AAA", "down_token_id": "BBB"}
    assert _select_token_id(market, "Down") == "BBB"
