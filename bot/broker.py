"""The only module that talks to Alpaca's trading API. Paper trading only.

The trading client is always built with paper=True and no URL override, and
the constructor checks the resulting base URL before anything else happens.
There is no code path to the live endpoint.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Protocol, TypeVar

from bot.config import Config, LiveTradingDisabledError
from bot.risk import Session

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"

T = TypeVar("T")


class BrokerError(RuntimeError):
    pass


class TransientBrokerError(BrokerError):
    """Network trouble, rate limit or server error: safe to retry reads, and
    safe to retry an order only after checking it didn't go through."""


@dataclass(frozen=True)
class BrokerAccount:
    equity: float
    cash: float
    last_equity: float       # equity at the previous close
    multiplier: float        # 1 = no margin
    shorting_enabled: bool


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: int
    avg_entry_price: float
    current_price: float | None = None
    unrealized_pl: float | None = None


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    client_order_id: str
    symbol: str
    side: str           # "buy" / "sell"
    type: str           # "market" / "stop" / ...
    qty: int
    status: str
    stop_price: float | None = None
    filled_qty: int = 0
    filled_avg_price: float | None = None


@dataclass(frozen=True)
class BrokerClock:
    now: datetime
    is_open: bool
    next_open: datetime
    next_close: datetime


class Broker(Protocol):
    """What the paper loop needs from a broker. Tests use a fake."""

    def get_account(self) -> BrokerAccount: ...
    def get_positions(self) -> dict[str, BrokerPosition]: ...
    def get_open_orders(self) -> list[BrokerOrder]: ...
    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def get_filled_sells(self, symbol: str, since: date) -> list[BrokerOrder]: ...
    def submit_market_buy_with_stop(self, symbol: str, qty: int, stop_price: float,
                                    client_order_id: str) -> BrokerOrder: ...
    def submit_market_sell(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrder: ...
    def submit_stop_sell(self, symbol: str, qty: int, stop_price: float,
                         client_order_id: str) -> BrokerOrder: ...
    def cancel_order(self, order_id: str) -> None: ...
    def get_clock(self) -> BrokerClock: ...
    def get_session(self, day: date) -> Session | None: ...
    def get_sessions(self, start: date, end: date) -> list[date]: ...


def assert_paper_client(client) -> None:
    base = getattr(client, "_base_url", None)
    base = getattr(base, "value", base)
    if str(base).rstrip("/") != PAPER_TRADING_URL:
        raise LiveTradingDisabledError(f"Trading client points at {base!r}, not the paper endpoint")


def _transient(e: Exception) -> bool:
    import requests

    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    status = getattr(e, "status_code", None)
    return status is not None and (status == 429 or status >= 500)


def with_retries(fn: Callable[[], T], attempts: int = 4, base_delay: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep) -> T:
    """Retry a read-only broker call with exponential backoff."""
    for i in range(attempts):
        try:
            return fn()
        except TransientBrokerError:
            if i == attempts - 1:
                raise
            sleep(base_delay * 2 ** i)
    raise AssertionError("unreachable")


class AlpacaPaperBroker:
    def __init__(self, cfg: Config, api_key: str, secret_key: str):
        if cfg.live_trading:
            raise LiveTradingDisabledError("live trading is not implemented in phase 1")
        from alpaca.trading.client import TradingClient

        # paper=True is hard-coded and there is deliberately no url_override.
        self.client = TradingClient(api_key, secret_key, paper=True)
        assert_paper_client(self.client)

    def _call(self, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except Exception as e:  # translate SDK/network errors
            if _transient(e):
                raise TransientBrokerError(str(e)) from e
            raise BrokerError(str(e)) from e

    @staticmethod
    def _order(o) -> BrokerOrder:
        v = lambda x: getattr(x, "value", x)  # noqa: E731
        return BrokerOrder(
            id=str(o.id), client_order_id=o.client_order_id, symbol=o.symbol, side=v(o.side),
            type=v(o.type) if o.type is not None else v(o.order_type), qty=int(float(o.qty or 0)),
            status=v(o.status), stop_price=float(o.stop_price) if o.stop_price else None,
            filled_qty=int(float(o.filled_qty or 0)),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
        )

    def get_account(self) -> BrokerAccount:
        a = self._call(self.client.get_account)
        return BrokerAccount(equity=float(a.equity), cash=float(a.cash), last_equity=float(a.last_equity),
                             multiplier=float(a.multiplier), shorting_enabled=bool(a.shorting_enabled))

    def get_positions(self) -> dict[str, BrokerPosition]:
        out = {}
        for p in self._call(self.client.get_all_positions):
            out[p.symbol] = BrokerPosition(
                p.symbol, int(float(p.qty)), float(p.avg_entry_price),
                current_price=float(p.current_price) if p.current_price else None,
                unrealized_pl=float(p.unrealized_pl) if p.unrealized_pl else None)
        return out

    def get_open_orders(self) -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False, limit=500)
        return [self._order(o) for o in self._call(lambda: self.client.get_orders(filter=req))]

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        try:
            return self._order(self._call(lambda: self.client.get_order_by_client_id(client_order_id)))
        except TransientBrokerError:
            raise
        except BrokerError as e:
            cause = e.__cause__
            if getattr(cause, "status_code", None) == 404:
                return None
            raise

    def get_filled_sells(self, symbol: str, since: date) -> list[BrokerOrder]:
        from datetime import time as dtime, timezone

        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.CLOSED, nested=False, limit=500, symbols=[symbol],
                               after=datetime.combine(since, dtime.min, tzinfo=timezone.utc))
        orders = [self._order(o) for o in self._call(lambda: self.client.get_orders(filter=req))]
        return [o for o in orders if o.side == "sell" and o.filled_qty > 0]

    def submit_market_buy_with_stop(self, symbol, qty, stop_price, client_order_id) -> BrokerOrder:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest

        # OTO: the stop is attached at submission and becomes active when the
        # entry fills. GTC so the stop doesn't expire at the end of the day.
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.GTC,
                                 order_class=OrderClass.OTO, client_order_id=client_order_id,
                                 stop_loss=StopLossRequest(stop_price=round(stop_price, 2)))
        return self._order(self._call(lambda: self.client.submit_order(req)))

    def submit_market_sell(self, symbol, qty, client_order_id) -> BrokerOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                                 client_order_id=client_order_id)
        return self._order(self._call(lambda: self.client.submit_order(req)))

    def submit_stop_sell(self, symbol, qty, stop_price, client_order_id) -> BrokerOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import StopOrderRequest

        req = StopOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL, time_in_force=TimeInForce.GTC,
                               stop_price=round(stop_price, 2), client_order_id=client_order_id)
        return self._order(self._call(lambda: self.client.submit_order(req)))

    def cancel_order(self, order_id: str) -> None:
        self._call(lambda: self.client.cancel_order_by_id(order_id))

    def get_clock(self) -> BrokerClock:
        c = self._call(self.client.get_clock)
        return BrokerClock(now=c.timestamp, is_open=c.is_open, next_open=c.next_open, next_close=c.next_close)

    def _calendar(self, start: date, end: date):
        from alpaca.trading.requests import GetCalendarRequest

        return self._call(lambda: self.client.get_calendar(GetCalendarRequest(start=start, end=end)))

    def get_session(self, day: date) -> Session | None:
        from zoneinfo import ZoneInfo

        ny = ZoneInfo("America/New_York")
        for c in self._calendar(day, day):
            if c.date == day:
                op, cl = c.open, c.close
                op = op if op.tzinfo else op.replace(tzinfo=ny)
                cl = cl if cl.tzinfo else cl.replace(tzinfo=ny)
                return Session(op, cl)
        return None

    def get_sessions(self, start: date, end: date) -> list[date]:
        return [c.date for c in self._calendar(start, end)]
