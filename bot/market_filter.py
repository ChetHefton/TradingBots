"""Market filter applied to every strategy: only open new positions when SPY's
close is above its 200-day SMA. Sells are never blocked by the filter."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot import indicators as ind
from bot.models import Side, Signal


@dataclass(frozen=True)
class FilterResult:
    risk_on: bool
    close: float | None
    sma: float | None


def evaluate(bars: dict[str, pd.DataFrame], symbol: str, sma_days: int) -> FilterResult:
    df = bars.get(symbol)
    if df is None or df.empty:
        return FilterResult(False, None, None)
    close = float(df["close"].iloc[-1])
    sma = ind.last(ind.sma(df["close"], sma_days))
    return FilterResult(risk_on=sma is not None and close > sma, close=close, sma=sma)


def apply(signals: list[Signal], result: FilterResult) -> tuple[list[Signal], list[Signal]]:
    """Return (kept, blocked)."""
    if result.risk_on:
        return list(signals), []
    kept = [s for s in signals if s.side is Side.SELL]
    blocked = [s for s in signals if s.side is Side.BUY]
    return kept, blocked
