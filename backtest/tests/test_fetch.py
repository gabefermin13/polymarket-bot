import pytest
import sys
sys.path.insert(0, 'C:/tmp/backtest')
from backtest_fetch import parse_asset, parse_outcome, parse_duration, compute_entry_ts

def test_parse_asset_btc():
    assert parse_asset("Will BTC be higher or lower in 5 minutes?") == "BTC"

def test_parse_asset_eth():
    assert parse_asset("Will ETH be higher or lower in 15 minutes?") == "ETH"

def test_parse_asset_doge():
    assert parse_asset("Will DOGE be higher or lower in 5 minutes?") == "DOGE"

def test_parse_asset_none():
    assert parse_asset("Will SOL be higher or lower?") is None

def test_parse_outcome_up_wins():
    assert parse_outcome(["Up", "Down"], ["0.99", "0.01"]) == "Up"

def test_parse_outcome_down_wins():
    assert parse_outcome(["Up", "Down"], ["0.01", "0.99"]) == "Down"

def test_parse_outcome_ambiguous():
    assert parse_outcome(["Up", "Down"], ["0.5", "0.5"]) is None

def test_parse_duration_5min():
    assert parse_duration(start=1000, end=1300) == "5min"

def test_parse_duration_15min():
    assert parse_duration(start=1000, end=1900) == "15min"

def test_parse_duration_unknown():
    assert parse_duration(start=1000, end=1600) is None

def test_compute_entry_ts_15min():
    assert compute_entry_ts(end_time=10000, duration_type="15min") == 10000 - 750

def test_compute_entry_ts_5min():
    assert compute_entry_ts(end_time=10000, duration_type="5min") == 10000 - 75
