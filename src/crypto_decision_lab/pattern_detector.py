"""Rule-based detector of bullish chart patterns *while they form* (E02 draft).

The detector is a causal state machine over closed hourly bars. After bar ``i``
closes it updates confirmed swing pivots and the active patterns; the resulting
state becomes the feature set of the event at bar ``i + 1`` (the first open that
can act on it). No value at event ``t`` depends on bar ``t`` or later bars.

Swing pivots are confirmed with a delay of ``pivot_bars``: a high at bar ``j``
is a pivot once bars ``j+1 .. j+pivot_bars`` have closed without exceeding it.
This delay is the price of causality and is part of every pattern's latency.

Patterns (long only, the engine trades LONG/FLAT):

* ``dbot`` - double bottom: lows L1, L2 within ``bottom_tolerance`` separated by
  a high H (the neckline) at least ``min_depth`` above them.
* ``atri`` - ascending triangle: two highs within ``top_tolerance`` (resistance)
  and two rising lows.
* ``flag`` - bull flag: an impulse (pole) of at least ``min_pole`` from a pivot
  low to a pivot high within ``max_pole_bars``, followed by a pullback that
  stays above ``max_retrace`` of the pole. Forms when the pole top is confirmed.
* ``rng`` - range (rectangle): two highs and two lows each within
  ``range_tolerance``, height at least ``min_depth``.

When a pattern ends, the next event carries ``pat_<id>_ended`` with the cause
(1 target, 2 invalidation, 3 age) for exactly one bar.

Each active pattern exposes ``progress`` (0 at the base, 1 at the neckline or
resistance, above 1 after the breakout), a geometric ``confidence`` in [0, 1],
the distance to its invalidation level and to its measured-move target. After
a breakout the invalidation level moves up to the neckline (failed breakout).
A pattern ends when a close reaches the target or the invalidation level, or
after ``max_age_bars``. A gap in the hourly calendar resets all state.

The engine, the strategies and the decision contract are not involved: this
module only produces features. :class:`PatternStrategy` reads them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Mapping

import pandas as pd

from .decision_contract import ACTION_LONG, utc
from .execution_contract import ACTION_FLAT, HOUR

DETECTOR_VERSION = "cdl.e02.pattern-detector.v1"
PATTERNS = ("dbot", "atri", "flag", "rng")
END_TARGET, END_INVALIDATION, END_AGE = 1.0, 2.0, 3.0
END_CAUSES = {END_TARGET: "target", END_INVALIDATION: "invalidation", END_AGE: "age"}
FEATURE_SUFFIXES = ("progress", "confidence", "stop_dist", "target_dist", "broken", "age")
CLOSE_ONLY = "close_only"


def feature_name(pattern: str, suffix: str) -> str:
    return f"pat_{pattern}_{suffix}"


@dataclass(frozen=True)
class DetectorParams:
    pivot_bars: int = 3
    max_span_bars: int = 120
    bottom_tolerance: float = 0.01
    top_tolerance: float = 0.005
    min_depth: float = 0.015
    min_rise: float = 0.003
    invalid_buffer: float = 0.002
    max_age_bars: int = 72
    min_pole: float = 0.04
    max_pole_bars: int = 24
    max_retrace: float = 0.5
    range_tolerance: float = 0.004

    def __post_init__(self) -> None:
        for name in ("pivot_bars", "max_span_bars", "max_age_bars", "max_pole_bars"):
            value = getattr(self, name)
            if int(value) != value or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("bottom_tolerance", "top_tolerance", "min_depth", "min_rise", "invalid_buffer",
                     "min_pole", "max_retrace", "range_tolerance"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0 < value < 1:
                raise ValueError(f"{name} must be in (0, 1)")

    def digest(self) -> str:
        encoded = json.dumps(dict(version=DETECTOR_VERSION, **asdict(self)), sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass
class _Pivot:
    index: int
    price: float
    kind: str  # "H" or "L"


@dataclass
class _Pattern:
    key: tuple
    base: float
    neckline: float
    invalidation: float
    target: float
    confidence: float
    formed_at: int
    broken: bool = False


class PatternDetector:
    """Incremental detector: call :meth:`update` once per closed bar, in order."""

    def __init__(self, params: DetectorParams | None = None):
        self.params = params or DetectorParams()
        self.reset()

    def reset(self) -> None:
        self.highs: list[float] = []
        self.lows: list[float] = []
        self.pivots: list[_Pivot] = []
        self.active: dict[str, _Pattern] = {}
        self.seen_keys: set[tuple] = set()
        self.ended: dict[str, float] = {}
        self.last_close: float | None = None

    # ------------------------------------------------------------- pivots
    def _confirm_pivots(self) -> bool:
        k = self.params.pivot_bars
        n = len(self.highs)
        j = n - 1 - k
        if j < k:
            return False
        new = False
        window_h = self.highs[j - k: j + k + 1]
        if self.highs[j] > max(self.highs[j - k: j]) and self.highs[j] >= max(window_h[k + 1:]):
            new |= self._add_pivot(_Pivot(j, self.highs[j], "H"))
        window_l = self.lows[j - k: j + k + 1]
        if self.lows[j] < min(self.lows[j - k: j]) and self.lows[j] <= min(window_l[k + 1:]):
            new |= self._add_pivot(_Pivot(j, self.lows[j], "L"))
        return new

    def _add_pivot(self, pivot: _Pivot) -> bool:
        # Keep pivots alternating: two consecutive pivots of the same kind keep the extreme one.
        if self.pivots and self.pivots[-1].kind == pivot.kind:
            last = self.pivots[-1]
            better = pivot.price > last.price if pivot.kind == "H" else pivot.price < last.price
            if not better:
                return False
            self.pivots[-1] = pivot
        else:
            self.pivots.append(pivot)
        horizon = len(self.highs) - 1 - 2 * self.params.max_span_bars
        self.pivots = [p for p in self.pivots if p.index >= horizon]
        return True

    # ------------------------------------------------------------- formation
    def _form_double_bottom(self, now: int) -> _Pattern | None:
        p = self.params
        if len(self.pivots) < 3:
            return None
        l1, h, l2 = self.pivots[-3:]
        if (l1.kind, h.kind, l2.kind) != ("L", "H", "L") or now - l1.index > p.max_span_bars:
            return None
        gap = abs(l2.price - l1.price) / l1.price
        top = max(l1.price, l2.price)
        if gap > p.bottom_tolerance or (h.price - top) / top < p.min_depth:
            return None
        base = min(l1.price, l2.price)
        return _Pattern(key=("dbot", l1.index, h.index, l2.index), base=base, neckline=h.price,
                        invalidation=base * (1 - p.invalid_buffer), target=h.price + (h.price - base),
                        confidence=1.0 - gap / p.bottom_tolerance, formed_at=now)

    def _form_ascending_triangle(self, now: int) -> _Pattern | None:
        p = self.params
        if len(self.pivots) < 4:
            return None
        last4 = self.pivots[-4:]
        highs = [x for x in last4 if x.kind == "H"]
        lows = [x for x in last4 if x.kind == "L"]
        if len(highs) != 2 or len(lows) != 2 or now - last4[0].index > p.max_span_bars:
            return None
        h1, h2 = highs
        l1, l2 = lows
        gap = abs(h2.price - h1.price) / h1.price
        resistance = max(h1.price, h2.price)
        if gap > p.top_tolerance or l2.price < l1.price * (1 + p.min_rise) or l2.price >= min(h1.price, h2.price):
            return None
        return _Pattern(key=("atri",) + tuple(x.index for x in last4), base=l2.price, neckline=resistance,
                        invalidation=l2.price * (1 - p.invalid_buffer), target=resistance + (resistance - l1.price),
                        confidence=1.0 - gap / p.top_tolerance, formed_at=now)

    def _form_flag(self, now: int) -> _Pattern | None:
        p = self.params
        if len(self.pivots) < 2:
            return None
        low, top = self.pivots[-2:]
        if (low.kind, top.kind) != ("L", "H") or top.index - low.index > p.max_pole_bars:
            return None
        pole = top.price - low.price
        strength = pole / low.price
        if strength < p.min_pole:
            return None
        base = top.price - p.max_retrace * pole
        return _Pattern(key=("flag", low.index, top.index), base=base, neckline=top.price,
                        invalidation=base * (1 - p.invalid_buffer), target=top.price + pole,
                        confidence=min(1.0, strength / (2 * p.min_pole)), formed_at=now)

    def _form_range(self, now: int) -> _Pattern | None:
        p = self.params
        if len(self.pivots) < 4:
            return None
        last4 = self.pivots[-4:]
        highs = [x.price for x in last4 if x.kind == "H"]
        lows = [x.price for x in last4 if x.kind == "L"]
        if len(highs) != 2 or len(lows) != 2 or now - last4[0].index > p.max_span_bars:
            return None
        top_gap = abs(highs[1] - highs[0]) / highs[0]
        bottom_gap = abs(lows[1] - lows[0]) / lows[0]
        support, resistance = min(lows), max(highs)
        if max(top_gap, bottom_gap) > p.range_tolerance or (resistance - support) / support < p.min_depth:
            return None
        return _Pattern(key=("rng",) + tuple(x.index for x in last4), base=support, neckline=resistance,
                        invalidation=support * (1 - p.invalid_buffer), target=resistance + (resistance - support),
                        confidence=1.0 - max(top_gap, bottom_gap) / p.range_tolerance, formed_at=now)

    # ------------------------------------------------------------- update
    def update(self, high: float, low: float, close: float) -> dict[str, float]:
        """Ingest one closed bar; return the features valid from the next open."""

        for value in (high, low, close):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("bar values must be finite and positive")
        self.highs.append(float(high))
        self.lows.append(float(low))
        self.last_close = float(close)
        now = len(self.highs) - 1
        self.ended = {}
        if self._confirm_pivots():
            formers = (("dbot", self._form_double_bottom), ("atri", self._form_ascending_triangle),
                       ("flag", self._form_flag), ("rng", self._form_range))
            for name, former in formers:
                candidate = former(now)
                if candidate is not None and name not in self.active and candidate.key not in self.seen_keys:
                    self.seen_keys.add(candidate.key)
                    self.active[name] = candidate
        for name in list(self.active):
            pattern = self.active[name]
            if not pattern.broken and close > pattern.neckline:
                pattern.broken = True
                pattern.invalidation = max(pattern.invalidation, pattern.neckline * (1 - self.params.invalid_buffer))
            cause = (END_TARGET if close >= pattern.target else END_INVALIDATION if close <= pattern.invalidation
                     else END_AGE if now - pattern.formed_at >= self.params.max_age_bars else None)
            if cause is not None:
                self.ended[name] = cause
                del self.active[name]
        return self.features()

    def features(self) -> dict[str, float]:
        out: dict[str, float] = {}
        close = self.last_close
        if close is None:
            return out
        now = len(self.highs) - 1
        for name, pattern in sorted(self.active.items()):
            out[feature_name(name, "progress")] = (close - pattern.base) / (pattern.neckline - pattern.base)
            out[feature_name(name, "confidence")] = pattern.confidence
            out[feature_name(name, "stop_dist")] = close / pattern.invalidation - 1.0
            out[feature_name(name, "target_dist")] = pattern.target / close - 1.0
            out[feature_name(name, "broken")] = 1.0 if pattern.broken else 0.0
            out[feature_name(name, "age")] = float(now - pattern.formed_at)
        for name, cause in sorted(self.ended.items()):
            out[feature_name(name, "ended")] = cause
        return out


def pattern_features(frame: pd.DataFrame, params: DetectorParams | None = None) -> dict[pd.Timestamp, dict[str, float]]:
    """Features per event timestamp, from the bars that closed strictly before it.

    The entry for bar ``t`` is the detector state after bar ``t - 1h`` closed.
    A missing hour resets the detector, so no pattern spans a gap.
    """
    detector = PatternDetector(params)
    out: dict[pd.Timestamp, dict[str, float]] = {}
    previous = None
    for ts, row in frame.sort_index().iterrows():
        ts = utc(ts)
        if previous is not None and ts != previous + HOUR:
            detector.reset()
            out[ts] = {}
        else:
            out[ts] = detector.features() if previous is not None else {}
        detector.update(float(row["high"]), float(row["low"]), float(row["close"]))
        previous = ts
    return out


def pattern_market_events(frame: pd.DataFrame, params: DetectorParams | None = None, start=None, end_exclusive=None):
    """S03 market events carrying ``momentum_24h`` plus the pattern features.

    Bars before ``start`` only warm up the detector, as for momentum.
    """
    from .simulation import MarketEvent, market_events

    features = pattern_features(frame, params)
    return [MarketEvent(event.timestamp, event.open, {**event.features, **features[utc(event.timestamp)]})
            for event in market_events(frame, start, end_exclusive)]


# ------------------------------------------------------------- strategy
@dataclass(frozen=True)
class PatternStrategy:
    """Dynamic LONG/FLAT strategy on patterns in formation.

    Flat: LONG when a configured pattern has ``confidence >= min_confidence``,
    progress in ``[entry_min_progress, entry_max_progress]`` (and a breakout if
    ``require_breakout``), and reward/risk ``target_dist / stop_dist`` at least
    ``min_reward_risk``. In position: hold while any configured pattern is
    alive; FLAT when all ended (target, invalidation or age). Without features
    the strategy never opens (fail closed).
    """

    patterns: tuple[str, ...] = PATTERNS
    min_confidence: float = 0.5
    entry_min_progress: float = 0.5
    entry_max_progress: float = 1.05
    require_breakout: bool = False
    min_reward_risk: float = 1.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "patterns", tuple(self.patterns))
        unknown = set(self.patterns) - set(PATTERNS)
        if not self.patterns or unknown:
            raise ValueError(f"patterns must be a non-empty subset of {PATTERNS}")
        values = (self.min_confidence, self.entry_min_progress, self.entry_max_progress, self.min_reward_risk)
        if not all(math.isfinite(float(v)) for v in values):
            raise ValueError("strategy parameters must be finite")
        if not 0 <= self.min_confidence <= 1 or self.entry_min_progress > self.entry_max_progress:
            raise ValueError("invalid confidence or progress window")
        if self.min_reward_risk < 0:
            raise ValueError("min_reward_risk must be non-negative")

    def _alive(self, features: Mapping[str, float]) -> list[str]:
        return [p for p in self.patterns
                if feature_name(p, "progress") in features and features.get(feature_name(p, "stop_dist"), 0) > 0]

    def _qualifies(self, pattern: str, f: Mapping[str, float]) -> tuple[bool, str]:
        get = lambda suffix: f.get(feature_name(pattern, suffix))  # noqa: E731
        progress, confidence, stop, target, broken = (get(s) for s in ("progress", "confidence", "stop_dist",
                                                                       "target_dist", "broken"))
        if None in (progress, confidence, stop, target, broken) or stop <= 0:
            return False, f"{pattern}: incomplete"
        reward_risk = target / stop
        ok = (confidence >= self.min_confidence
              and self.entry_min_progress <= progress <= self.entry_max_progress
              and (broken >= 1 or not self.require_breakout)
              and reward_risk >= self.min_reward_risk)
        return ok, (f"{pattern}: progress={progress:.3f} confidence={confidence:.3f} "
                    f"rr={reward_risk:.2f} broken={int(broken)}")

    def decide(self, cutoff, features, portfolio):
        from .simulation import Proposal

        if CLOSE_ONLY in features:
            return Proposal(ACTION_FLAT, "evaluation window closed")
        if portfolio.quantity:
            alive = self._alive(features)
            if alive:
                return Proposal(ACTION_LONG, f"hold: pattern alive ({', '.join(alive)})")
            ended = [f"{p} {END_CAUSES.get(features.get(feature_name(p, 'ended')), 'unknown')}"
                     for p in self.patterns if feature_name(p, "ended") in features]
            return Proposal(ACTION_FLAT, f"exit: no configured pattern alive ({', '.join(ended) or 'none ended now'})")
        reasons = []
        for pattern in self.patterns:
            ok, reason = self._qualifies(pattern, features)
            if ok:
                return Proposal(ACTION_LONG, f"entry {reason}")
            if feature_name(pattern, "progress") in features:
                reasons.append(reason)
        return Proposal(ACTION_FLAT, "; ".join(reasons) if reasons else "no pattern in formation")


__all__ = [
    "DETECTOR_VERSION",
    "DetectorParams",
    "PATTERNS",
    "PatternDetector",
    "PatternStrategy",
    "feature_name",
    "pattern_features",
    "pattern_market_events",
]
