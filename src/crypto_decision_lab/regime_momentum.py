"""E03: momentum with hysteresis and a trend-regime filter.

Two ideas from the E01 outcome (REJECTED), where almost every exit came from
the momentum signal crossing back under its entry threshold and the holding
horizon never bound:

* **Hysteresis.**  Enter when ``momentum_24h > entry_threshold``; once in the
  market stay long while ``momentum_24h >= exit_threshold`` (with
  ``exit_threshold <= entry_threshold``).  A signal oscillating around a single
  threshold no longer opens and closes a round trip each time.
* **Regime filter.**  Never long when ``trend_ratio <= regime_min``, where
  ``trend_ratio = close(t-1h) / SMA_n(t-1h) - 1`` on the hourly calendar.

Both features use only bars closed before the event.  A missing hour inside
the SMA window leaves ``trend_ratio`` undefined; with any feature missing the
strategy proposes FLAT (fail closed, also when already long).
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

import pandas as pd

from .decision_contract import utc

TREND_FEATURE = "trend_ratio"


def trend_features(frame: pd.DataFrame, sma_hours: int) -> dict[pd.Timestamp, dict[str, float]]:
    """``trend_ratio`` per event timestamp from closes strictly before it.

    Computed on the hourly calendar: the SMA needs ``sma_hours`` consecutive
    closed bars, so a gap makes the feature undefined until the window refills.
    """
    if int(sma_hours) < 2:
        raise ValueError("sma_hours must be at least 2")
    close = frame["close"].astype(float)
    hourly = close.asfreq("1h") if len(close) else close
    sma = hourly.rolling(int(sma_hours), min_periods=int(sma_hours)).mean()
    ratio = (hourly / sma - 1.0).shift(1).reindex(frame.index)
    return {utc(ts): ({TREND_FEATURE: float(value)} if pd.notna(value) else {}) for ts, value in ratio.items()}


@dataclass(frozen=True)
class RegimeMomentumStrategy:
    entry_threshold: float = 0.03
    exit_threshold: float = 0.0
    regime_min: float = 0.0
    feature: str = "momentum_24h"
    regime_feature: str = TREND_FEATURE

    def __post_init__(self) -> None:
        for name in ("entry_threshold", "exit_threshold", "regime_min"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if float(self.exit_threshold) > float(self.entry_threshold):
            raise ValueError("exit_threshold must not exceed entry_threshold (hysteresis)")

    def decide(self, cutoff, features: Mapping[str, float], portfolio):
        from .simulation import ACTION_FLAT, ACTION_LONG, Proposal

        momentum = features.get(self.feature)
        trend = features.get(self.regime_feature)
        holding = float(getattr(portfolio, "quantity", 0.0)) > 0
        if momentum is None or trend is None:
            missing = [n for n, v in ((self.feature, momentum), (self.regime_feature, trend)) if v is None]
            return Proposal(ACTION_FLAT, f"missing feature {', '.join(missing)}: fail closed")
        regime_on = trend > self.regime_min
        state = f"{self.feature}={momentum:.6f}; {self.regime_feature}={trend:.6f}"
        if not regime_on:
            return Proposal(ACTION_FLAT, f"regime off ({self.regime_feature} <= {self.regime_min}); {state}")
        if holding:
            if momentum >= self.exit_threshold:
                return Proposal(ACTION_LONG, f"hold: {self.feature} >= exit {self.exit_threshold}; {state}")
            return Proposal(ACTION_FLAT, f"exit: {self.feature} < exit {self.exit_threshold}; {state}")
        if momentum > self.entry_threshold:
            return Proposal(ACTION_LONG, f"entry: {self.feature} > entry {self.entry_threshold}; {state}")
        return Proposal(ACTION_FLAT, f"no entry: {self.feature} <= entry {self.entry_threshold}; {state}")
