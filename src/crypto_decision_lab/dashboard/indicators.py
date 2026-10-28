"""Technical indicators for the read-only Crypto Decision Lab dashboard.

Design rules
------------
* Every indicator is a causal transformation of the supplied OHLCV frame.
* No function may shift future values backwards, interpolate missing bars or
  fetch data from outside the frame.
* Functions operate on the timeframe requested by the caller.  The dashboard
  resamples OHLCV *before* calling these functions.
* Third-party indicators can be added with :func:`register_indicator`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Literal

import numpy as np
import pandas as pd

IndicatorKind = Literal["overlay", "oscillator", "volume"]

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class IndicatorSpec:
    """Metadata used by the UI and by the computation registry."""

    key: str
    label: str
    kind: IndicatorKind
    default_params: dict[str, float | int | bool]
    outputs: tuple[str, ...]
    description: str = ""
    params_help: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IndicatorRequest:
    """A concrete indicator instance, including user-selected parameters."""

    key: str
    params: dict[str, float | int | bool] = field(default_factory=dict)
    label: str | None = None


@dataclass(frozen=True)
class IndicatorResult:
    request: IndicatorRequest
    spec: IndicatorSpec
    data: pd.DataFrame
    label: str


_REGISTRY: dict[str, tuple[IndicatorSpec, Callable[..., pd.DataFrame]]] = {}


def register_indicator(
    spec: IndicatorSpec,
    func: Callable[..., pd.DataFrame],
    *,
    replace: bool = False,
) -> None:
    """Register a causal indicator.

    ``func`` must accept the OHLCV frame as first argument and return a
    DataFrame indexed like the input frame.  Extra keyword arguments are the
    parameters declared in ``spec.default_params``.
    """

    if not replace and spec.key in _REGISTRY:
        raise ValueError(f"Indicator already registered: {spec.key}")
    _REGISTRY[spec.key] = (spec, func)


def indicator_specs() -> dict[str, IndicatorSpec]:
    return {key: spec for key, (spec, _) in _REGISTRY.items()}


def compute_indicator(
    frame: pd.DataFrame,
    key: str,
    params: dict[str, float | int | bool] | None = None,
) -> pd.DataFrame:
    """Compute one indicator using only the rows supplied in ``frame``."""

    _validate_frame(frame)
    if key not in _REGISTRY:
        raise KeyError(f"Unknown indicator: {key}")
    spec, func = _REGISTRY[key]
    merged = {**spec.default_params, **(params or {})}
    unknown = set(merged) - set(spec.default_params)
    if unknown:
        raise ValueError(f"Unknown parameters for {key}: {sorted(unknown)}")
    if "window" in merged and int(merged["window"]) < 1:
        raise ValueError("window must be >= 1")
    if "period" in merged and int(merged["period"]) < 1:
        raise ValueError("period must be >= 1")
    result = func(frame, **merged)
    if not isinstance(result, pd.DataFrame):
        raise TypeError("Indicator function must return a pandas DataFrame")
    if not result.index.equals(frame.index):
        raise ValueError("Indicator result index must match input index")
    return result


def compute_indicators(
    frame: pd.DataFrame, requests: Iterable[IndicatorRequest]
) -> list[IndicatorResult]:
    """Compute a list of requests, preserving order and duplicate periods."""

    output: list[IndicatorResult] = []
    for request in requests:
        spec = _REGISTRY.get(request.key, (None, None))[0]
        if spec is None:
            raise KeyError(f"Unknown indicator: {request.key}")
        data = compute_indicator(frame, request.key, request.params)
        output.append(
            IndicatorResult(
                request=request,
                spec=spec,
                data=data,
                label=format_indicator_label(request, spec),
            )
        )
    return output


def format_indicator_label(request: IndicatorRequest, spec: IndicatorSpec) -> str:
    if request.label:
        return request.label
    params = request.params or {}
    if not params:
        return spec.label
    shown = ", ".join(f"{key}={value}" for key, value in params.items())
    return f"{spec.label} ({shown})"


def _validate_frame(frame: pd.DataFrame) -> None:
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Missing OHLCV columns: {missing}")
    if frame.empty:
        raise ValueError("Cannot compute indicators on an empty frame")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("OHLCV frame must use a DatetimeIndex")
    if frame.index.tz is None:
        raise ValueError("OHLCV index must be timezone-aware UTC")
    if not frame.index.is_monotonic_increasing:
        raise ValueError("OHLCV index must be sorted")
    if frame.index.has_duplicates:
        raise ValueError("OHLCV index must not contain duplicates")


def _sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window=int(window), min_periods=int(window)).mean()


def _ema(close: pd.Series, span: int) -> pd.Series:
    span = int(span)
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    period = int(period)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = rsi.where(avg_loss != 0.0, 100.0)
    rsi = rsi.where(~((avg_gain == 0.0) & (avg_loss == 0.0)), 50.0)
    return rsi


def _true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    ranges = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _atr_raw(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    period = int(period)
    return _true_range(frame).ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()


def _macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    fast, slow, signal = int(fast), int(slow), int(signal)
    if fast >= slow:
        raise ValueError("MACD fast period must be smaller than slow period")
    macd = _ema(close, fast) - _ema(close, slow)
    signal_line = macd.ewm(
        span=signal, adjust=False, min_periods=signal
    ).mean()
    return pd.DataFrame(
        {
            "macd": macd,
            "macd_signal": signal_line,
            "macd_hist": macd - signal_line,
        }
    )


def _bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    window = int(window)
    middle = close.rolling(window=window, min_periods=window).mean()
    std = close.rolling(window=window, min_periods=window).std(ddof=0)
    return pd.DataFrame(
        {
            "bb_mid": middle,
            "bb_upper": middle + float(num_std) * std,
            "bb_lower": middle - float(num_std) * std,
        }
    )


def _adx(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    period = int(period)
    high, low = frame["high"], frame["low"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0.0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0.0), 0.0)
    atr = _atr_raw(frame, period)
    plus_smoothed = plus_dm.ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()
    minus_smoothed = minus_dm.ewm(
        alpha=1.0 / period, adjust=False, min_periods=period
    ).mean()
    plus_di = 100.0 * plus_smoothed / atr.replace(0.0, np.nan)
    minus_di = 100.0 * minus_smoothed / atr.replace(0.0, np.nan)
    denominator = (plus_di + minus_di).replace(0.0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / denominator).fillna(0.0)
    adx = dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    return pd.DataFrame({"adx": adx, "plus_di": plus_di, "minus_di": minus_di})


def _stochastic(
    frame: pd.DataFrame, k_window: int = 14, d_window: int = 3, smooth: int = 3
) -> pd.DataFrame:
    k_window, d_window, smooth = int(k_window), int(d_window), int(smooth)
    rolling_low = frame["low"].rolling(k_window, min_periods=k_window).min()
    rolling_high = frame["high"].rolling(k_window, min_periods=k_window).max()
    spread = (rolling_high - rolling_low).replace(0.0, np.nan)
    raw_k = 100.0 * (frame["close"] - rolling_low) / spread
    k = raw_k.rolling(smooth, min_periods=smooth).mean()
    d = k.rolling(d_window, min_periods=d_window).mean()
    return pd.DataFrame({"stoch_k": k, "stoch_d": d})


def _rolling_volatility(
    close: pd.Series,
    window: int = 24,
    annualize: bool = True,
    periods_per_year: int | None = None,
) -> pd.DataFrame:
    window = int(window)
    log_returns = np.log(close / close.shift(1))
    volatility = log_returns.rolling(window, min_periods=window).std(ddof=1)
    if annualize:
        if periods_per_year is None:
            periods_per_year = _infer_periods_per_year(close.index)
        if periods_per_year:
            volatility = volatility * np.sqrt(float(periods_per_year))
    return pd.DataFrame({"rolling_volatility": volatility})


def _infer_periods_per_year(index: pd.DatetimeIndex) -> int | None:
    if len(index) < 2:
        return None
    deltas = pd.Series(index).diff().dt.total_seconds().dropna()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return None
    median_seconds = float(deltas.median())
    if median_seconds <= 0:
        return None
    return int(round(365.0 * 24.0 * 3600.0 / median_seconds))


def _registered() -> None:
    register_indicator(
        IndicatorSpec(
            "sma",
            "SMA",
            "overlay",
            {"window": 20},
            ("sma",),
            "Simple moving average of close.",
        ),
        lambda frame, window=20: pd.DataFrame(
            {"sma": _sma(frame["close"], int(window))}
        ),
    )
    register_indicator(
        IndicatorSpec(
            "ema",
            "EMA",
            "overlay",
            {"window": 20},
            ("ema",),
            "Exponential moving average of close, adjust=False.",
        ),
        lambda frame, window=20: pd.DataFrame(
            {"ema": _ema(frame["close"], int(window))}
        ),
    )
    register_indicator(
        IndicatorSpec(
            "rsi",
            "RSI",
            "oscillator",
            {"period": 14},
            ("rsi",),
            "Wilder relative strength index (0-100).",
        ),
        lambda frame, period=14: pd.DataFrame(
            {"rsi": _rsi(frame["close"], int(period))}
        ),
    )
    register_indicator(
        IndicatorSpec(
            "macd",
            "MACD",
            "oscillator",
            {"fast": 12, "slow": 26, "signal": 9},
            ("macd", "macd_signal", "macd_hist"),
            "MACD line, signal line and histogram.",
        ),
        lambda frame, fast=12, slow=26, signal=9: _macd(
            frame["close"], int(fast), int(slow), int(signal)
        ),
    )
    register_indicator(
        IndicatorSpec(
            "bollinger",
            "Bollinger Bands",
            "overlay",
            {"window": 20, "num_std": 2.0},
            ("bb_mid", "bb_upper", "bb_lower"),
            "Rolling mean with volatility bands.",
        ),
        lambda frame, window=20, num_std=2.0: _bollinger(
            frame["close"], int(window), float(num_std)
        ),
    )
    register_indicator(
        IndicatorSpec(
            "adx",
            "ADX",
            "oscillator",
            {"period": 14},
            ("adx", "plus_di", "minus_di"),
            "Wilder average directional index with +DI/-DI.",
        ),
        lambda frame, period=14: _adx(frame, int(period)),
    )
    register_indicator(
        IndicatorSpec(
            "roc",
            "ROC",
            "oscillator",
            {"period": 12},
            ("roc",),
            "Rate of change in percent over N bars.",
        ),
        lambda frame, period=12: pd.DataFrame(
            {"roc": frame["close"].pct_change(int(period)) * 100.0}
        ),
    )
    register_indicator(
        IndicatorSpec(
            "stochastic",
            "Stochastic",
            "oscillator",
            {"k_window": 14, "d_window": 3, "smooth": 3},
            ("stoch_k", "stoch_d"),
            "Slow stochastic oscillator.",
        ),
        lambda frame, k_window=14, d_window=3, smooth=3: _stochastic(
            frame, int(k_window), int(d_window), int(smooth)
        ),
    )
    register_indicator(
        IndicatorSpec(
            "atr",
            "ATR",
            "oscillator",
            {"period": 14},
            ("atr",),
            "Average true range in price units.",
        ),
        lambda frame, period=14: pd.DataFrame({"atr": _atr_raw(frame, int(period))}),
    )
    register_indicator(
        IndicatorSpec(
            "rolling_volatility",
            "Rolling Volatility",
            "oscillator",
            {"window": 24, "annualize": True, "periods_per_year": 0},
            ("rolling_volatility",),
            "Rolling standard deviation of log returns; optional annualization.",
        ),
        lambda frame, window=24, annualize=True, periods_per_year=0: _rolling_volatility(
            frame["close"],
            int(window),
            bool(annualize),
            int(periods_per_year) or None,
        ),
    )
    register_indicator(
        IndicatorSpec(
            "volume",
            "Volume",
            "volume",
            {},
            ("volume",),
            "Raw base-asset volume.",
        ),
        lambda frame: pd.DataFrame({"volume": frame["volume"].astype(float)}),
    )
    register_indicator(
        IndicatorSpec(
            "volume_ma",
            "Volume MA",
            "volume",
            {"window": 20},
            ("volume_ma",),
            "Simple moving average of volume.",
        ),
        lambda frame, window=20: pd.DataFrame(
            {"volume_ma": frame["volume"].rolling(int(window), min_periods=int(window)).mean()}
        ),
    )


DEFAULT_PRESETS: tuple[dict, ...] = (
    {"key": "sma", "params": {"window": 7}},
    {"key": "sma", "params": {"window": 20}},
    {"key": "sma", "params": {"window": 50}},
    {"key": "sma", "params": {"window": 100}},
    {"key": "sma", "params": {"window": 200}},
    {"key": "ema", "params": {"window": 9}},
    {"key": "ema", "params": {"window": 12}},
    {"key": "ema", "params": {"window": 26}},
    {"key": "ema", "params": {"window": 50}},
    {"key": "ema", "params": {"window": 200}},
    {"key": "macd", "params": {"fast": 12, "slow": 26, "signal": 9}},
    {"key": "bollinger", "params": {"window": 20, "num_std": 2.0}},
    {"key": "adx", "params": {"period": 14}},
    {"key": "rsi", "params": {"period": 14}},
    {"key": "roc", "params": {"period": 12}},
    {"key": "stochastic", "params": {"k_window": 14, "d_window": 3, "smooth": 3}},
    {"key": "atr", "params": {"period": 14}},
    {"key": "rolling_volatility", "params": {"window": 24, "annualize": True, "periods_per_year": 0}},
    {"key": "volume", "params": {}},
    {"key": "volume_ma", "params": {"window": 20}},
)


_registered()
