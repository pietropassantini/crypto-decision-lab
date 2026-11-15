"""E04: long-only mean reversion inside an uptrend ("buy the dip").

A different signal family from momentum (S08, E01, E03) and from the
breakout patterns of E02: instead of following strength it buys short-term
weakness, and only while the longer trend is up.

Features, all from bars closed before the event, on the hourly calendar:

* ``zscore`` = (close(t-1h) - SMA_w(t-1h)) / STD_w(t-1h) over ``window_hours``;
* ``zscore_prev`` = the same one hour earlier (to detect the crossing);
* ``trend_ratio`` from :func:`regime_momentum.trend_features`.

Rules:

* **Entry** (flat): the z-score crosses below ``-entry_z``
  (``zscore_prev >= -entry_z`` and ``zscore < -entry_z``) while
  ``trend_ratio > regime_min``.  One entry per dip: staying oversold does not
  re-trigger, so a horizon exit is not followed by an immediate re-entry.
* **Exit** (long): ``zscore >= exit_z`` (price back at its mean).  The engine's
  holding horizon is the time stop.  The regime is checked at entry only: a dip
  may push the price under its long average without forcing the exit.
* Any feature needed for the decision missing -> FLAT (fail closed).
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping

import pandas as pd

from .decision_contract import utc
from .regime_momentum import TREND_FEATURE, trend_features

ZSCORE = "zscore"
ZSCORE_PREV = "zscore_prev"


def zscore_features(frame: pd.DataFrame, window_hours: int) -> dict[pd.Timestamp, dict[str, float]]:
    """``zscore`` and ``zscore_prev`` per event timestamp from closes strictly before it.

    A missing hour inside the window, or a flat window (zero deviation), leaves
    the value undefined.
    """
    window = int(window_hours)
    if window < 2:
        raise ValueError("window_hours must be at least 2")
    close = frame["close"].astype(float)
    hourly = close.asfreq("1h") if len(close) else close
    mean = hourly.rolling(window, min_periods=window).mean()
    std = hourly.rolling(window, min_periods=window).std(ddof=0)
    z = ((hourly - mean) / std.where(std > 0)).shift(1)
    current, previous = z.reindex(frame.index), z.shift(1).reindex(frame.index)
    out = {}
    for ts in frame.index:
        features = {}
        if pd.notna(current.loc[ts]):
            features[ZSCORE] = float(current.loc[ts])
        if pd.notna(previous.loc[ts]):
            features[ZSCORE_PREV] = float(previous.loc[ts])
        out[utc(ts)] = features
    return out


def reversion_features(frame: pd.DataFrame, window_hours: int, regime_sma_hours: int) -> dict:
    """z-score features merged with the trend regime feature."""
    z = zscore_features(frame, window_hours)
    trend = trend_features(frame, regime_sma_hours)
    return {ts: {**z[ts], **trend[ts]} for ts in z}


@dataclass(frozen=True)
class MeanReversionStrategy:
    entry_z: float = 2.0
    exit_z: float = 0.0
    regime_min: float = 0.0

    def __post_init__(self) -> None:
        for name in ("entry_z", "exit_z", "regime_min"):
            if not math.isfinite(float(getattr(self, name))):
                raise ValueError(f"{name} must be finite")
        if float(self.entry_z) <= 0:
            raise ValueError("entry_z must be positive (entry below -entry_z)")
        if float(self.exit_z) <= -float(self.entry_z):
            raise ValueError("exit_z must be above -entry_z")

    def decide(self, cutoff, features: Mapping[str, float], portfolio):
        from .simulation import ACTION_FLAT, ACTION_LONG, Proposal

        z = features.get(ZSCORE)
        holding = float(getattr(portfolio, "quantity", 0.0)) > 0
        if z is None:
            return Proposal(ACTION_FLAT, f"missing feature {ZSCORE}: fail closed")
        if holding:
            if z >= self.exit_z:
                return Proposal(ACTION_FLAT, f"exit: reverted, {ZSCORE}={z:.4f} >= {self.exit_z}")
            return Proposal(ACTION_LONG, f"hold: {ZSCORE}={z:.4f} < exit {self.exit_z}")
        previous, trend = features.get(ZSCORE_PREV), features.get(TREND_FEATURE)
        if previous is None or trend is None:
            missing = [n for n, v in ((ZSCORE_PREV, previous), (TREND_FEATURE, trend)) if v is None]
            return Proposal(ACTION_FLAT, f"missing feature {', '.join(missing)}: fail closed")
        if trend <= self.regime_min:
            return Proposal(ACTION_FLAT, f"no entry: regime off ({TREND_FEATURE}={trend:.4f})")
        if previous >= -self.entry_z and z < -self.entry_z:
            return Proposal(ACTION_LONG, f"entry: {ZSCORE} crossed below -{self.entry_z} "
                                         f"({previous:.4f} -> {z:.4f}); {TREND_FEATURE}={trend:.4f}")
        return Proposal(ACTION_FLAT, f"no entry: no cross below -{self.entry_z} ({ZSCORE}={z:.4f})")
