"""Technical indicators on daily bar DataFrames.

Every function only looks backward: the value on day t uses data up to and
including day t's close, never later.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, days: int) -> pd.Series:
    return series.rolling(days, min_periods=days).mean()


def atr(bars: pd.DataFrame, days: int) -> pd.Series:
    """Average true range, simple average of the true range (Wilder-style
    smoothing is a common alternative; a simple mean is easier to verify)."""
    prev_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            bars["high"] - bars["low"],
            (bars["high"] - prev_close).abs(),
            (bars["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.rolling(days, min_periods=days).mean()


def rsi(close: pd.Series, days: int) -> pd.Series:
    """Wilder's RSI (exponential smoothing with alpha = 1/days, seeded with the
    first change). Returns 100 when the window had no losses, 50 when the
    price did not move, and NaN until `days` changes are available."""
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= days:
        return pd.Series(out, index=close.index)
    delta = np.diff(c)
    gain = np.clip(delta, 0, None)
    loss = np.clip(-delta, 0, None)
    alpha = 1.0 / days
    g, l_ = gain[0], loss[0]
    for i in range(1, len(delta)):
        g += alpha * (gain[i] - g)
        l_ += alpha * (loss[i] - l_)
        if i >= days - 1:
            if l_ == 0:
                out[i + 1] = 50.0 if g == 0 else 100.0
            else:
                out[i + 1] = 100.0 - 100.0 / (1.0 + g / l_)
    return pd.Series(out, index=close.index)


def momentum_12_1(close: pd.Series, lookback_days: int, skip_days: int) -> float | None:
    """Return from `lookback_days` ago to `skip_days` ago, using only the
    series as given (its last value is "today"). None if not enough history."""
    if len(close) < lookback_days + 1:
        return None
    start = close.iloc[-(lookback_days + 1)]
    end = close.iloc[-(skip_days + 1)]
    if start <= 0 or pd.isna(start) or pd.isna(end):
        return None
    return float(end / start - 1)


def volatility(close: pd.Series, days: int) -> float | None:
    """Standard deviation of daily returns over the last `days` days."""
    rets = close.pct_change().dropna()
    if len(rets) < days:
        return None
    return float(rets.iloc[-days:].std(ddof=1))


def last(series: pd.Series) -> float | None:
    if series.empty:
        return None
    v = series.iloc[-1]
    return None if pd.isna(v) else float(v)
