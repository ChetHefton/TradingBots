"""Backtester: next-open execution, no look-ahead, costs, stops, risk limits."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot.backtest.engine import buy_and_hold, run_backtest
from bot.backtest.metrics import compute, max_drawdown
from bot.data import synthetic_bars
from bot.models import Side, Signal
from bot.strategies import build_strategy
from bot.strategies.base import Strategy, StrategyContext


class BuyOnce(Strategy):
    """Buys `symbol` at the close of `on` (fills next open)."""
    name = "buyonce"

    def __init__(self, symbol, on, stop_distance=5.0, weight=0.10):
        super().__init__({})
        self.symbol, self.on, self.stop_distance, self.weight = symbol, on, stop_distance, weight
        self.seen_last_bar: list[tuple[date, date]] = []

    def generate(self, ctx: StrategyContext):
        self.seen_last_bar.append((ctx.as_of, ctx.bars[self.symbol].index[-1].date()))
        if ctx.as_of == self.on and self.symbol not in ctx.positions:
            return [Signal(self.symbol, Side.BUY, "test", target_weight=self.weight,
                           stop_distance=self.stop_distance)]
        return []


def flat_market(cfg, n=400, start="2020-01-01"):
    """SPY rising gently so the market filter is on; AAA flat at 100."""
    idx = pd.bdate_range(start, periods=n)
    spy = pd.DataFrame({"open": np.linspace(100, 140, n), "close": np.linspace(100, 140, n)}, index=idx)
    spy["high"], spy["low"], spy["volume"] = spy["close"] * 1.001, spy["open"] * 0.999, 1e6
    aaa = pd.DataFrame({"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1e6}, index=idx)
    return {"SPY": spy, "AAPL": aaa}


def test_signal_on_close_fills_at_next_open_with_costs(cfg):
    bars = flat_market(cfg)
    idx = bars["SPY"].index
    decide = idx[300].date()
    bars["AAPL"].loc[idx[301], "open"] = 101.0
    strat = BuyOnce("AAPL", decide)
    r = run_backtest(strat, bars, cfg, idx[250].date(), idx[-1].date())
    buy = r.fills[0]
    assert buy.date == idx[301].date()
    assert buy.price == pytest.approx(101.0 * (1 + cfg.costs.per_side), rel=1e-6)


def test_strategy_never_sees_future_bars(cfg):
    bars = flat_market(cfg)
    idx = bars["SPY"].index
    strat = BuyOnce("AAPL", idx[300].date())
    run_backtest(strat, bars, cfg, idx[250].date(), idx[-1].date())
    assert strat.seen_last_bar and all(as_of == last for as_of, last in strat.seen_last_bar)


def test_changing_future_data_does_not_change_the_past(cfg):
    syms = list(cfg.symbols)
    bars = synthetic_bars(syms, date(2016, 1, 1), date(2020, 12, 31), seed=3)
    cut = pd.Timestamp("2019-06-28")
    altered = {s: df.copy() for s, df in bars.items()}
    for df in altered.values():
        df.loc[df.index > cut, ["open", "high", "low", "close"]] *= 3.0  # wild future
    for name in ("momentum", "rsi2", "faber"):
        a = run_backtest(build_strategy(name, cfg.strategy_params(name)), bars, cfg, date(2017, 3, 1), date(2020, 12, 31))
        b = run_backtest(build_strategy(name, cfg.strategy_params(name)), altered, cfg, date(2017, 3, 1), date(2020, 12, 31))
        pd.testing.assert_series_equal(a.equity.loc[:cut], b.equity.loc[:cut])


def test_stop_gap_down_fills_at_open(cfg):
    bars = flat_market(cfg)
    idx = bars["SPY"].index
    strat = BuyOnce("AAPL", idx[300].date(), stop_distance=5.0)  # stop at 95
    gap_day = idx[305]
    bars["AAPL"].loc[gap_day, ["open", "high", "low", "close"]] = [90.0, 91.0, 89.0, 90.0]
    r = run_backtest(strat, bars, cfg, idx[250].date(), idx[-1].date())
    sell = [f for f in r.fills if f.side == "sell"][0]
    assert sell.date == gap_day.date()
    assert sell.price == pytest.approx(90.0 * (1 - cfg.costs.per_side))
    assert "stop" in sell.reason


def test_stop_intraday_fills_at_stop(cfg):
    bars = flat_market(cfg)
    idx = bars["SPY"].index
    strat = BuyOnce("AAPL", idx[300].date(), stop_distance=5.0)
    day = idx[305]
    bars["AAPL"].loc[day, ["open", "high", "low", "close"]] = [99.0, 99.5, 94.0, 96.0]
    r = run_backtest(strat, bars, cfg, idx[250].date(), idx[-1].date())
    sell = [f for f in r.fills if f.side == "sell"][0]
    assert sell.price == pytest.approx(95.0 * (1 - cfg.costs.per_side))


def test_risk_engine_caps_position_in_backtest(cfg):
    bars = flat_market(cfg)
    idx = bars["SPY"].index
    strat = BuyOnce("AAPL", idx[300].date(), stop_distance=0.5, weight=1.0)
    r = run_backtest(strat, bars, cfg, idx[250].date(), idx[-1].date())
    buy = r.fills[0]
    assert buy.qty * 100.0 <= cfg.risk.max_position_pct * cfg.backtest["initial_capital"] + 1e-6


def test_market_filter_blocks_entries_in_backtest(cfg):
    bars = flat_market(cfg)
    bars["SPY"][["open", "high", "low", "close"]] = bars["SPY"][["open", "high", "low", "close"]].iloc[::-1].values
    idx = bars["SPY"].index
    r = run_backtest(BuyOnce("AAPL", idx[300].date()), bars, cfg, idx[250].date(), idx[-1].date())
    assert r.fills == []


def test_buy_and_hold_and_metrics(cfg):
    bars = flat_market(cfg)
    idx = bars["SPY"].index
    r = buy_and_hold(bars["SPY"], cfg, idx[0].date(), idx[-1].date())
    m = compute(r)
    assert m.total_return == pytest.approx(140 / (100 * (1 + cfg.costs.per_side)) - 1, abs=1e-3)
    assert m.time_in_market == 1.0
    assert max_drawdown(pd.Series([100, 120, 90, 130])) == pytest.approx(-0.25)
