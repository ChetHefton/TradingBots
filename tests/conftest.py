from __future__ import annotations

import itertools
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from bot.broker import BrokerAccount, BrokerClock, BrokerOrder, BrokerPosition
from bot.config import load_config
from bot.risk import Session

NY = ZoneInfo("America/New_York")


@pytest.fixture
def cfg():
    return load_config()


def make_session(day: date = date(2026, 10, 5)) -> Session:
    return Session(datetime(day.year, day.month, day.day, 9, 30, tzinfo=NY),
                   datetime(day.year, day.month, day.day, 16, 0, tzinfo=NY))


def mid_session(day: date = date(2026, 10, 5)) -> datetime:
    return datetime(day.year, day.month, day.day, 11, 0, tzinfo=NY)


def bars_from_closes(closes, start="2020-01-01", spread=0.01) -> pd.DataFrame:
    """Daily bars from a list of closes: open = previous close, high/low a
    fixed fraction around the bar."""
    idx = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=idx, dtype=float)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.concat([open_, close], axis=1).max(axis=1) * (1 + spread)
    low = pd.concat([open_, close], axis=1).min(axis=1) * (1 - spread)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": 1e6})


class FakeBroker:
    """In-memory broker that records every call. Market orders fill instantly."""

    def __init__(self, equity=100_000.0, cash=100_000.0, last_equity=None, positions=None, prices=None):
        self.equity = equity
        self.cash = cash
        self.last_equity = last_equity if last_equity is not None else equity
        self.positions: dict[str, BrokerPosition] = positions or {}
        self.prices = prices or {}
        self.orders: dict[str, BrokerOrder] = {}
        self.submissions: list[str] = []
        self.cancelled: list[str] = []
        self.ids = itertools.count(1)
        self.fail_next_submits = 0       # raise transient error but still accept the order
        self.fail_next_submits_lost = 0  # raise transient error and drop the order

    # reads
    def get_account(self):
        return BrokerAccount(self.equity, self.cash, self.last_equity, 1.0, False)

    def get_positions(self):
        return dict(self.positions)

    def get_open_orders(self):
        return [o for o in self.orders.values() if o.status in ("new", "accepted", "held")]

    def get_order_by_client_id(self, cid):
        return self.orders.get(cid)

    def get_filled_sells(self, symbol, since):
        return [o for o in self.orders.values() if o.symbol == symbol and o.side == "sell" and o.status == "filled"]

    def get_clock(self):
        now = mid_session()
        return BrokerClock(now, True, now + timedelta(days=1), now + timedelta(hours=5))

    def get_session(self, day):
        return make_session(day)

    def get_sessions(self, start, end):
        return [d.date() for d in pd.bdate_range(start, end)]

    # writes
    def _add(self, cid, symbol, side, type_, qty, status, stop=None, price=None):
        from bot.broker import TransientBrokerError

        self.submissions.append(cid)
        o = BrokerOrder(str(next(self.ids)), cid, symbol, side, type_, qty, status, stop,
                        qty if status == "filled" else 0, price if status == "filled" else None)
        if self.fail_next_submits_lost:
            self.fail_next_submits_lost -= 1
            raise TransientBrokerError("timeout (order lost)")
        self.orders[cid] = o
        if self.fail_next_submits:
            self.fail_next_submits -= 1
            raise TransientBrokerError("timeout (order accepted)")
        return o

    def submit_market_buy_with_stop(self, symbol, qty, stop_price, cid):
        price = self.prices.get(symbol, 100.0)
        o = self._add(cid, symbol, "buy", "market", qty, "filled", price=price)
        self.positions[symbol] = BrokerPosition(symbol, qty, price)
        self.cash -= qty * price
        self._add(cid + "-sl", symbol, "sell", "stop", qty, "new", stop=stop_price)
        return o

    def submit_market_sell(self, symbol, qty, cid):
        price = self.prices.get(symbol, 100.0)
        o = self._add(cid, symbol, "sell", "market", qty, "filled", price=price)
        self.positions.pop(symbol, None)
        self.cash += qty * price
        return o

    def submit_stop_sell(self, symbol, qty, stop_price, cid):
        return self._add(cid, symbol, "sell", "stop", qty, "new", stop=stop_price)

    def cancel_order(self, order_id):
        for cid, o in self.orders.items():
            if o.id == order_id:
                self.orders[cid] = BrokerOrder(o.id, o.client_order_id, o.symbol, o.side, o.type, o.qty,
                                               "canceled", o.stop_price)
                self.cancelled.append(cid)


class FakeData:
    def __init__(self, bars: dict[str, pd.DataFrame], latest: dict[str, float] | None = None):
        self.bars = bars
        self.latest = latest or {}

    def get_daily_bars(self, symbols, start, end=None):
        out = {}
        for s in symbols:
            if s in self.bars:
                df = self.bars[s]
                out[s] = df.loc[pd.Timestamp(start):pd.Timestamp(end) if end else None]
        return out

    def get_latest_price(self, symbol):
        return self.latest.get(symbol)
