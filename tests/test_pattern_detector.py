"""E02 draft: causal pattern detector and dynamic pattern strategy (synthetic data only)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.execution_contract import ExecutionHypothesis
from crypto_decision_lab.pattern_detector import (
    DetectorParams,
    PatternStrategy,
    feature_name,
    pattern_features,
    pattern_market_events,
)
from crypto_decision_lab.simulation import Portfolio, RunConfig, run, select_strategy


def _frame(closes, start="2024-01-01"):
    closes = np.asarray(closes, dtype=float)
    index = pd.date_range(start, periods=len(closes), freq="h", tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])
    return pd.DataFrame(dict(open=opens, high=closes * 1.001, low=closes * 0.999, close=closes,
                             volume=1.0), index=index)


def _path(*points, steps=8):
    """Piecewise-linear closes through ``points``, ``steps`` bars per segment."""
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        out.extend(np.linspace(a, b, steps + 1)[1:])
    return out


DOUBLE_BOTTOM = _path(100, 90, 95, 90.3, 96, 103)
TRIANGLE = _path(95, 90, 100, 94, 100.2, 97, 104)


def _active(features, pattern):
    return [ts for ts, f in features.items() if feature_name(pattern, "progress") in f]


# ------------------------------------------------------------- detection
def test_double_bottom_forms_breaks_out_and_completes():
    frame = _frame(DOUBLE_BOTTOM)
    features = pattern_features(frame)
    active = _active(features, "dbot")
    assert active, "double bottom not detected"
    first = features[active[0]]
    # Formed after L2 is confirmed (pivot delay), before the neckline breakout.
    assert 0 <= first[feature_name("dbot", "progress")] < 1
    assert first[feature_name("dbot", "broken")] == 0
    assert 0.5 < first[feature_name("dbot", "confidence")] <= 1
    assert any(features[ts][feature_name("dbot", "broken")] == 1 for ts in active)
    # Target reached before the end of the path: the pattern is gone.
    assert active[-1] < frame.index[-1]
    assert feature_name("dbot", "progress") not in features[frame.index[-1]]


def test_double_bottom_invalidated_below_base():
    frame = _frame(_path(100, 90, 95, 90.3, 92, 85))
    features = pattern_features(frame)
    active = _active(features, "dbot")
    assert active
    last_close_before_exit = frame["close"].shift(1).loc[active[-1]]
    assert last_close_before_exit > 90 * 0.999 * (1 - DetectorParams().invalid_buffer)
    assert feature_name("dbot", "progress") not in features[frame.index[-1]]


def test_ascending_triangle_detected():
    features = pattern_features(_frame(TRIANGLE))
    active = _active(features, "atri")
    assert active
    assert not _active(features, "dbot")  # lows 4% apart: not a double bottom
    assert features[active[0]][feature_name("atri", "confidence")] > 0


def _ended(features, pattern):
    return [f[feature_name(pattern, "ended")] for f in features.values() if feature_name(pattern, "ended") in f]


def test_end_cause_reported_for_one_bar():
    assert _ended(pattern_features(_frame(DOUBLE_BOTTOM)), "dbot") == [1.0]  # target
    assert _ended(pattern_features(_frame(_path(100, 90, 95, 90.3, 92, 85))), "dbot") == [2.0]  # invalidation


def test_bull_flag_detected_and_completes():
    features = pattern_features(_frame(_path(102, 100, 106, 103.5, 113, 114)))
    active = _active(features, "flag")
    assert active
    first = features[active[0]]
    assert first[feature_name("flag", "progress")] > 0.5  # pullback still in the upper half of the pole
    assert first[feature_name("flag", "confidence")] > 0.5  # pole 6% vs min_pole 4%
    assert _ended(features, "flag") == [1.0]


def test_flag_invalidated_by_deep_retrace():
    features = pattern_features(_frame(_path(102, 100, 106, 101, 104)))
    assert _active(features, "flag")
    assert _ended(features, "flag") == [2.0]


def test_weak_pole_is_not_a_flag():
    assert not _active(pattern_features(_frame(_path(102, 100, 102.5, 101.5, 104))), "flag")


def test_range_detected():
    features = pattern_features(_frame(_path(98, 95, 100, 95.2, 100.1, 96, 104)))
    active = _active(features, "rng")
    assert active
    # Lows 95 -> 95.2 are flat (below min_rise): no triangle on the same four pivots. The later
    # rising low (96) legitimately forms one afterwards.
    assert feature_name("atri", "progress") not in features[active[0]]
    assert 0 < features[active[0]][feature_name("rng", "confidence")] <= 1


def test_no_pattern_on_monotonic_series():
    features = pattern_features(_frame(np.linspace(100, 120, 200)))
    assert all(not f for f in features.values())


# ------------------------------------------------------------- causality
def _noisy():
    rng = np.random.default_rng(7)
    walk = 100 * np.exp(np.cumsum(rng.normal(0, 0.006, 260)))
    return _frame(np.concatenate([walk, np.asarray(DOUBLE_BOTTOM) * walk[-1] / 100]))


def test_features_are_prefix_invariant():
    frame = _noisy()
    full = pattern_features(frame)
    assert sum(bool(f) for f in full.values()) > 0
    for cut in range(5, len(frame), 11):
        prefix = pattern_features(frame.iloc[:cut])
        assert all(prefix[ts] == full[ts] for ts in prefix)


def test_current_and_future_bars_do_not_change_features():
    frame = _noisy()
    full = pattern_features(frame)
    for position in (40, 150, len(frame) - 30):
        ts = frame.index[position]
        changed = frame.copy()
        changed.iloc[position:, changed.columns.get_indexer(["high", "low", "close"])] *= 1.2
        altered = pattern_features(changed)
        assert all(altered[t] == full[t] for t in frame.index[: position + 1])
        assert altered[ts] == full[ts]


def test_gap_resets_detector():
    frame = _frame(DOUBLE_BOTTOM)
    cut = len(frame) - 10
    gapped = pd.concat([frame.iloc[:cut], frame.iloc[cut + 1:]])
    features = pattern_features(gapped)
    assert features[gapped.index[cut]] == {}
    assert all(not f for f in list(features.values())[cut:])


def test_events_carry_momentum_and_pattern_features():
    frame = _frame(DOUBLE_BOTTOM)
    events = pattern_market_events(frame)
    features = pattern_features(frame)
    assert [e.timestamp for e in events] == list(frame.index)
    for event in events:
        assert {k: v for k, v in event.features.items() if k.startswith("pat_")} == features[event.timestamp]


# ------------------------------------------------------------- strategy
def _f(progress=0.7, confidence=0.8, stop=0.03, target=0.08, broken=0.0, pattern="dbot"):
    values = dict(progress=progress, confidence=confidence, stop_dist=stop, target_dist=target,
                  broken=broken, age=3.0)
    return {feature_name(pattern, k): v for k, v in values.items()}


FLAT, LONG = Portfolio(1000.0), Portfolio(0.0, 1.0)


def test_strategy_entry_rules():
    strategy = PatternStrategy()
    assert strategy.decide(None, _f(), FLAT).exposure == "LONG"
    assert strategy.decide(None, {}, FLAT).exposure == "FLAT"
    assert strategy.decide(None, _f(progress=0.2), FLAT).exposure == "FLAT"
    assert strategy.decide(None, _f(confidence=0.3), FLAT).exposure == "FLAT"
    assert strategy.decide(None, _f(target=0.03), FLAT).exposure == "FLAT"  # rr 1 < 1.5
    assert PatternStrategy(require_breakout=True).decide(None, _f(), FLAT).exposure == "FLAT"
    assert PatternStrategy(require_breakout=True).decide(None, _f(broken=1.0), FLAT).exposure == "LONG"
    assert PatternStrategy(patterns=("atri",)).decide(None, _f(), FLAT).exposure == "FLAT"


def test_strategy_holds_while_pattern_alive_and_exits_after():
    strategy = PatternStrategy()
    assert strategy.decide(None, _f(progress=1.4), LONG).exposure == "LONG"
    assert strategy.decide(None, {}, LONG).exposure == "FLAT"
    assert strategy.decide(None, {**_f(), "close_only": 1.0}, LONG).exposure == "FLAT"


@pytest.mark.parametrize("kwargs", [dict(patterns=()), dict(patterns=("hs",)), dict(min_confidence=2),
                                    dict(entry_min_progress=1, entry_max_progress=0.5),
                                    dict(min_reward_risk=float("nan"))])
def test_strategy_rejects_invalid_parameters(kwargs):
    with pytest.raises(ValueError):
        PatternStrategy(**kwargs)


def test_detector_params_validation_and_digest():
    with pytest.raises(ValueError):
        DetectorParams(pivot_bars=0)
    with pytest.raises(ValueError):
        DetectorParams(bottom_tolerance=0)
    assert DetectorParams().digest() == DetectorParams().digest() != DetectorParams(pivot_bars=4).digest()


def test_engine_round_trip_on_double_bottom():
    assert isinstance(select_strategy({"name": "pattern"}), PatternStrategy)
    config = RunConfig(strategy={"name": "pattern", "parameters": {"patterns": ["dbot"]}},
                       hypothesis=ExecutionHypothesis(horizon_hours=500))
    result = run(pattern_market_events(_frame(DOUBLE_BOTTOM)), config)
    executions = [r for r in result.ledger if r["kind"] == "execution"]
    assert [e["side"] for e in executions] == ["BUY", "SELL"]
    exits = [r for r in result.ledger if r["kind"] == "decision" and r["reason"].startswith("exit:")]
    assert exits and "dbot target" in exits[0]["reason"]
    end = result.ledger[-1]
    assert end["kind"] == "end" and not end["position_open"]
    assert end["equity"] > config.initial_cash  # synthetic path ends above the measured-move target
