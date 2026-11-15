"""E05: daily-bar trend following on the hourly engine.

E01-E04 showed that on hourly decisions the gross result per trade (0.2-0.9 on
a 250 position) never clears the round-trip cost (about 0.75).  E05 keeps the
cost and lengthens the horizon: one decision per UTC day, on features built
from daily closes, so each trade aims at a multi-day move.

The engine still steps hourly (gap handling, fills and costs are unchanged).
Features exist only on the 00:00 UTC event of each day:

* ``daily_decision`` = 1.0 marks the decision bar;
* ``d_momentum`` = close(D-1) / close(D-1-n) - 1 over ``momentum_days``;
* ``d_trend`` = close(D-1) / SMA_m(daily closes up to D-1) - 1 over ``sma_days``.

The daily close of day D-1 is the close of its 23:00 bar, i.e. the last bar
closed before the decision.  A day whose 23:00 bar is missing has no daily
close, and every window containing it is undefined.

Rules (hysteresis and regime, as in E03, on daily features):

* not a decision bar -> keep the current exposure;
* decision bar with a missing feature -> FLAT (fail closed);
* ``d_trend <= regime_min`` -> FLAT;
* flat: LONG when ``d_momentum > entry_threshold``;
* long: stay while ``d_momentum >= exit_threshold``.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

import pandas as pd

from .decision_contract import utc

DECISION = "daily_decision"
MOMENTUM = "d_momentum"
TREND = "d_trend"


def daily_closes(frame: pd.DataFrame) -> pd.Series:
    """Daily closes indexed by the decision timestamp (00:00 UTC of the following day)."""
    close = frame["close"].astype(float)
    hourly = close.asfreq("1h") if len(close) else close
    last = hourly[hourly.index.hour == 23]
    if not len(last):
        return last
    daily = last.copy()
    daily.index = daily.index + pd.Timedelta(hours=1)
    return daily.asfreq("1D")


def daily_features(frame: pd.DataFrame, momentum_days: int, sma_days: int) -> dict[pd.Timestamp, dict[str, float]]:
    """Features per event timestamp; only 00:00 UTC events carry any."""
    momentum_days, sma_days = int(momentum_days), int(sma_days)
    if momentum_days < 1 or sma_days < 2:
        raise ValueError("momentum_days must be >= 1 and sma_days >= 2")
    daily = daily_closes(frame)
    momentum = daily / daily.shift(momentum_days) - 1.0
    trend = daily / daily.rolling(sma_days, min_periods=sma_days).mean() - 1.0
    out: dict[pd.Timestamp, dict[str, float]] = {}
    for ts in frame.index:
        ts = utc(ts)
        if ts.hour != 0:
            out[ts] = {}
            continue
        features = {DECISION: 1.0}
        if ts in momentum.index and pd.notna(momentum.loc[ts]):
            features[MOMENTUM] = float(momentum.loc[ts])
        if ts in trend.index and pd.notna(trend.loc[ts]):
            features[TREND] = float(trend.loc[ts])
        out[ts] = features
    return out


@dataclass(frozen=True)
class DailyTrendStrategy:
    entry_threshold: float = 0.0
    exit_threshold: float = 0.0
    regime_min: float = 0.0

    def __post_init__(self) -> None:
        for name in ("entry_threshold", "exit_threshold", "regime_min"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if float(self.exit_threshold) > float(self.entry_threshold):
            raise ValueError("exit_threshold must not exceed entry_threshold (hysteresis)")

    def decide(self, cutoff, features: Mapping[str, float], portfolio):
        from .simulation import ACTION_FLAT, ACTION_LONG, Proposal

        holding = float(getattr(portfolio, "quantity", 0.0)) > 0
        if features.get(DECISION) is None:
            return Proposal(ACTION_LONG if holding else ACTION_FLAT, "between daily decisions: keep")
        momentum, trend = features.get(MOMENTUM), features.get(TREND)
        if momentum is None or trend is None:
            missing = [n for n, v in ((MOMENTUM, momentum), (TREND, trend)) if v is None]
            return Proposal(ACTION_FLAT, f"missing feature {', '.join(missing)}: fail closed")
        state = f"{MOMENTUM}={momentum:.6f}; {TREND}={trend:.6f}"
        if trend <= self.regime_min:
            return Proposal(ACTION_FLAT, f"regime off ({TREND} <= {self.regime_min}); {state}")
        if holding:
            if momentum >= self.exit_threshold:
                return Proposal(ACTION_LONG, f"hold: {MOMENTUM} >= exit {self.exit_threshold}; {state}")
            return Proposal(ACTION_FLAT, f"exit: {MOMENTUM} < exit {self.exit_threshold}; {state}")
        if momentum > self.entry_threshold:
            return Proposal(ACTION_LONG, f"entry: {MOMENTUM} > entry {self.entry_threshold}; {state}")
        return Proposal(ACTION_FLAT, f"no entry: {MOMENTUM} <= entry {self.entry_threshold}; {state}")
