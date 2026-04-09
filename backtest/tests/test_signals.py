import pytest
import sys
sys.path.insert(0, 'C:/tmp/backtest')

from backtest_signals import (
    classify_s1_from_candles,
    classify_s3_from_funding,
    classify_s6_from_clob,
    neutral_signal,
    aggregate_s5,
)


def test_classify_s1_up():
    candles = [{"open": 100, "close": 100}, {"open": 100, "close": 101},
               {"open": 101, "close": 102}, {"open": 102, "close": 103},
               {"open": 103, "close": 105}]
    direction, conf = classify_s1_from_candles(candles)
    assert direction == "UP"
    assert 0.55 <= conf <= 0.75


def test_classify_s1_down():
    candles = [{"open": 105, "close": 105}, {"open": 105, "close": 103},
               {"open": 103, "close": 101}, {"open": 101, "close": 100},
               {"open": 100, "close": 98}]
    direction, conf = classify_s1_from_candles(candles)
    assert direction == "DOWN"


def test_classify_s1_flat():
    candles = [{"open": 100, "close": 100}] * 5
    direction, conf = classify_s1_from_candles(candles)
    assert direction == "NEUTRAL"


def test_classify_s3_positive_funding_up():
    direction, conf = classify_s3_from_funding(funding_rate=0.0003)
    assert direction == "UP"


def test_classify_s3_negative_funding_down():
    direction, conf = classify_s3_from_funding(funding_rate=-0.0003)
    assert direction == "DOWN"


def test_classify_s3_neutral():
    direction, conf = classify_s3_from_funding(funding_rate=0.00001)
    assert direction == "NEUTRAL"


def test_classify_s6_net_positive_up():
    direction, conf = classify_s6_from_clob(net_volume=500.0)
    assert direction == "UP"


def test_classify_s6_net_negative_down():
    direction, conf = classify_s6_from_clob(net_volume=-500.0)
    assert direction == "DOWN"


def test_classify_s6_near_zero_balanced():
    direction, conf = classify_s6_from_clob(net_volume=10.0)
    assert direction == "BALANCED"


def test_neutral_signal():
    d, c = neutral_signal()
    assert d == "NEUTRAL"
    assert c == 0.5


def test_aggregate_s5_majority_up():
    results = [("UP", 0.6), ("UP", 0.6), ("DOWN", 0.6)]
    direction, conf = aggregate_s5(results, primary_asset_result=("UP", 0.6))
    assert direction == "UP"
    assert conf == pytest.approx(0.60)


def test_aggregate_s5_no_majority():
    results = [("UP", 0.6), ("DOWN", 0.6), ("NEUTRAL", 0.5)]
    direction, conf = aggregate_s5(results, primary_asset_result=("UP", 0.6))
    assert direction == "NEUTRAL"
