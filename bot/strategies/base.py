"""Strategy interface.

A strategy looks at daily bars up to and including `as_of` (a completed
session) and returns signals to execute at the next session's open. It never
sees the broker, never sizes orders and never sees data after `as_of`: the
caller truncates the bars before handing them over.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Any

import pandas as pd

from bot.models import Position, Signal


@dataclass(frozen=True)
class StrategyContext:
    as_of: date                      # last completed session; all bars end here
    next_session: date               # the session the signals will execute in
    sessions: pd.DatetimeIndex       # trading days up to and including next_session
    bars: dict[str, pd.DataFrame]    # symbol -> daily bars, index <= as_of
    positions: dict[str, Position]   # current holdings
    universe: tuple[str, ...]        # tradable symbols (allowlist)


class Strategy(ABC):
    name: str = "base"

    def __init__(self, params: dict[str, Any]):
        self.params = params

    @abstractmethod
    def generate(self, ctx: StrategyContext) -> list[Signal]:
        ...

    def warmup_days(self) -> int:
        """Bars of history needed before the first valid signal."""
        return 0


def session_index_in_month(day: date, sessions: pd.DatetimeIndex) -> int:
    """0 for the first trading day of `day`'s month, 1 for the second, etc."""
    ts = pd.Timestamp(day)
    month = sessions[(sessions.year == ts.year) & (sessions.month == ts.month)]
    month = month[month <= ts]
    if len(month) == 0 or month[-1] != ts:
        raise ValueError(f"{day} is not in the session calendar")
    return len(month) - 1


def is_monthly_trade_day(next_session: date, sessions: pd.DatetimeIndex, offset: int) -> bool:
    """True when `next_session` is the (offset+1)-th trading day of its month,
    i.e. the decision made at today's close executes on the scheduled day."""
    return session_index_in_month(next_session, sessions) == offset
