"""Plain data types shared by strategies, the risk engine, the backtester and
the paper loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Position:
    symbol: str
    qty: int
    entry_price: float
    entry_date: date
    stop_price: float | None = None


@dataclass(frozen=True)
class Signal:
    """What a strategy wants. It is a request, not an order: the market filter
    and the risk engine decide whether and how much to trade."""

    symbol: str
    side: Side
    reason: str
    # BUY only: desired position value as a fraction of equity (the risk
    # engine may cut it) and the stop distance in price units.
    target_weight: float = 0.0
    stop_distance: float = 0.0
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccountView:
    equity: float
    cash: float
    positions: dict[str, Position]
