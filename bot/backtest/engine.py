"""Daily-bar backtester that runs the same strategy, market filter and risk
engine code as the paper loop.

Timeline for each session d:
  1. Open: start the risk day, mark equity at the open, run the risk checks,
     and fill orders decided at the previous close at d's open (plus costs).
     Sells go first, then buys. Buys are sized from d's open, like the paper
     loop sizing from the live price after the open.
  2. Intraday: a position whose low touches its stop is sold at the stop, or
     at the open if it gapped below the stop.
  3. Close: mark equity at the close and feed it to the risk engine.
  4. Decide: the strategy sees bars up to d's close only; its signals wait
     for d+1's open. That one-bar delay is what prevents look-ahead.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from bot import market_filter
from bot.config import Config
from bot.models import AccountView, Position, Side, Signal
from bot.risk import RiskEngine, Session
from bot.strategies.base import Strategy, StrategyContext
from bot.trade_log import Fill

NY = ZoneInfo("America/New_York")
HISTORY_PADDING = 60


def history_bars_needed(strategy: Strategy, filter_sma_days: int) -> int:
    """How many bars the strategy is given each day. The paper loop uses the
    same number so live signals match backtest signals."""
    return max(strategy.warmup_days(), filter_sma_days, 252) + HISTORY_PADDING


@dataclass
class ClosedTrade:
    symbol: str
    entry_date: date
    exit_date: date
    qty: int
    entry_price: float
    exit_price: float
    pnl: float
    exit_reason: str


@dataclass
class BacktestResult:
    name: str
    equity: pd.Series
    in_market: pd.Series
    trades: list[ClosedTrade] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    events: list[tuple[date, str, str]] = field(default_factory=list)
    rejected_orders: int = 0
    entries: int = 0
    initial_capital: float = 0.0


class _Sim:
    def __init__(self, strategy: Strategy, cfg: Config, capital: float):
        self.strategy = strategy
        self.cfg = cfg
        self.cost = cfg.costs.per_side
        self.cash = capital
        self.positions: dict[str, Position] = {}
        self.lot_ids: dict[str, str] = {}
        self.fills: list[Fill] = []
        self.trades: list[ClosedTrade] = []
        self.events: list[tuple[date, str, str]] = []
        self.rejected = 0
        self.entries = 0
        self.risk = RiskEngine(cfg.risk, cfg.symbols, cost_per_side=self.cost)

    def mark(self, prices: dict[str, float]) -> float:
        return self.cash + sum(p.qty * prices[s] for s, p in self.positions.items())

    def account(self, equity: float) -> AccountView:
        return AccountView(equity=equity, cash=self.cash, positions=dict(self.positions))

    def buy(self, day: date, sym: str, qty: int, ref: float, stop: float, reason: str) -> None:
        price = ref * (1 + self.cost)
        self.cash -= qty * price
        fid = f"{self.strategy.name}-{len(self.fills)}"
        self.positions[sym] = Position(sym, qty, price, day, stop)
        self.lot_ids[sym] = fid
        self.fills.append(Fill(fid, day, sym, "buy", qty, round(price, 4), self.strategy.name, reason))
        self.entries += 1

    def sell(self, day: date, sym: str, ref: float, reason: str) -> None:
        pos = self.positions.pop(sym)
        price = ref * (1 - self.cost)
        self.cash += pos.qty * price
        pnl = (price - pos.entry_price) * pos.qty
        fid = f"{self.strategy.name}-{len(self.fills)}"
        self.fills.append(Fill(fid, day, sym, "sell", pos.qty, round(price, 4), self.strategy.name, reason,
                               lot_id=self.lot_ids.pop(sym), realized_pnl=round(pnl, 2)))
        self.trades.append(ClosedTrade(sym, pos.entry_date, day, pos.qty, pos.entry_price, price, pnl, reason))


def run_backtest(strategy: Strategy, bars: dict[str, pd.DataFrame], cfg: Config,
                 start: date, end: date, name: str | None = None) -> BacktestResult:
    capital = float(cfg.backtest["initial_capital"])
    mf = cfg.market_filter
    calendar = bars[mf["symbol"]].index
    all_sessions = calendar
    run_days = calendar[(calendar >= pd.Timestamp(start)) & (calendar <= pd.Timestamp(end))]
    if len(run_days) == 0:
        raise ValueError(f"No sessions between {start} and {end}")
    n_hist = history_bars_needed(strategy, mf["sma_days"])
    universe = tuple(s for s in cfg.symbols if s in bars)

    # Per-symbol arrays for fast lookups.
    frames = {s: bars[s] for s in universe}
    last_close: dict[str, float] = {}

    sim = _Sim(strategy, cfg, capital)
    pending: list[Signal] = []
    equity_curve, in_market = {}, {}
    prev_close_equity = capital

    for day_ts in run_days:
        day = day_ts.date()
        bar_today = {s: frames[s].loc[day_ts] for s in universe if day_ts in frames[s].index}
        session_open = datetime.combine(day, time(9, 30), tzinfo=NY)
        session = Session(session_open, datetime.combine(day, time(16, 0), tzinfo=NY))
        now = session_open + timedelta(minutes=cfg.risk.skip_open_minutes + 1)

        # 1. Open
        sim.risk.start_day(day, prev_close_equity)
        open_prices = {s: float(bar_today[s]["open"]) if s in bar_today else last_close[s]
                       for s in sim.positions}
        open_equity = sim.mark(open_prices)
        for ev in sim.risk.update_equity(open_equity):
            sim.events.append((day, ev.kind, ev.message))

        for sig in [s for s in pending if s.side is Side.SELL]:
            if sig.symbol not in sim.positions or sig.symbol not in bar_today:
                continue
            d = sim.risk.check_exit(sig.symbol, sim.positions[sig.symbol].qty,
                                    sim.account(open_equity), now, session)
            if not d.approved:
                sim.rejected += 1
                continue
            sim.risk.record_order()
            sim.sell(day, sig.symbol, float(bar_today[sig.symbol]["open"]), sig.reason)

        for sig in [s for s in pending if s.side is Side.BUY]:
            if sig.symbol not in bar_today:
                continue
            ref = float(bar_today[sig.symbol]["open"])
            stop = ref - sig.stop_distance
            d = sim.risk.check_entry(sig.symbol, ref, stop, sig.target_weight,
                                     sim.account(open_equity), now, session)
            if not d.approved:
                sim.rejected += 1
                continue
            sim.risk.record_order()
            sim.buy(day, sig.symbol, d.qty, ref, stop, sig.reason)
        pending = []

        # 2. Intraday stops (protective stops rest at the broker; no risk check).
        for sym in list(sim.positions):
            pos = sim.positions[sym]
            if sym not in bar_today or pos.stop_price is None:
                continue
            b = bar_today[sym]
            if float(b["low"]) <= pos.stop_price:
                fill_ref = min(float(b["open"]), pos.stop_price)
                sim.sell(day, sym, fill_ref, f"hard stop hit at {pos.stop_price:.2f}")

        # 3. Close
        for s, b in bar_today.items():
            last_close[s] = float(b["close"])
        close_equity = sim.mark({s: last_close[s] for s in sim.positions})
        for ev in sim.risk.update_equity(close_equity):
            sim.events.append((day, ev.kind, ev.message))
        equity_curve[day_ts] = close_equity
        in_market[day_ts] = bool(sim.positions)
        prev_close_equity = close_equity

        # 4. Decide for the next session
        k = all_sessions.get_loc(day_ts)
        if day_ts == run_days[-1] or k + 1 >= len(all_sessions) or sim.risk.state.kill_switch:
            continue
        hist = {}
        for s in universe:
            df = frames[s]
            j = df.index.searchsorted(day_ts, side="right")
            hist[s] = df.iloc[max(0, j - n_hist):j]
        ctx = StrategyContext(
            as_of=day, next_session=all_sessions[k + 1].date(), sessions=all_sessions[: k + 2],
            bars=hist, positions=dict(sim.positions), universe=universe,
        )
        signals = strategy.generate(ctx)
        fr = market_filter.evaluate(hist, mf["symbol"], mf["sma_days"])
        pending, _blocked = market_filter.apply(signals, fr)

    return BacktestResult(
        name=name or strategy.name,
        equity=pd.Series(equity_curve, name="equity"),
        in_market=pd.Series(in_market, name="in_market"),
        trades=sim.trades, fills=sim.fills, events=sim.events,
        rejected_orders=sim.rejected, entries=sim.entries, initial_capital=capital,
    )


def buy_and_hold(bars: pd.DataFrame, cfg: Config, start: date, end: date, name: str = "buy & hold SPY") -> BacktestResult:
    """Benchmark: all-in at the first open (with costs), no risk limits."""
    capital = float(cfg.backtest["initial_capital"])
    df = bars.loc[pd.Timestamp(start):pd.Timestamp(end)]
    price = float(df["open"].iloc[0]) * (1 + cfg.costs.per_side)
    qty = int(np.floor(capital / price))
    cash = capital - qty * price
    equity = cash + qty * df["close"]
    return BacktestResult(name=name, equity=equity.rename("equity"),
                          in_market=pd.Series(True, index=df.index), entries=1,
                          initial_capital=capital)
