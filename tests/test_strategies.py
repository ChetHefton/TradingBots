"""Strategy signals on small hand-built price series where the answer is obvious."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from bot import indicators as ind
from bot import market_filter
from bot.models import Position, Side, Signal
from bot.strategies import build_strategy
from bot.strategies.base import StrategyContext, is_monthly_trade_day, session_index_in_month
from tests.conftest import bars_from_closes

END = "2021-06-30"  # a month-end session; next session 2021-07-01 is the 1st of the month


def trend_bars(daily_growth: float, n: int = 300, noise: float = 0.002, end: str = END, crash=None):
    idx = pd.bdate_range(end=end, periods=n)
    steps = np.full(n, daily_growth)
    steps[1::2] += noise
    steps[0::2] -= noise
    close = 100 * np.exp(np.cumsum(steps))
    if crash:
        days, frac = crash
        close[-days:] = close[-days - 1] * np.linspace(1 - frac / days, 1 - frac, days)
    df = bars_from_closes(close)
    df.index = idx
    return df


def ctx_for(bars, positions=None, next_session=date(2021, 7, 1)):
    idx = next(iter(bars.values())).index
    sessions = idx.append(pd.DatetimeIndex([pd.Timestamp(next_session)]))
    return StrategyContext(as_of=idx[-1].date(), next_session=next_session, sessions=sessions,
                           bars=bars, positions=positions or {}, universe=tuple(bars))


@pytest.fixture
def momentum(cfg):
    return build_strategy("momentum", cfg.strategy_params("momentum"))


# ---- indicators -------------------------------------------------------------------

def test_momentum_12_1_skips_latest_month():
    close = pd.Series(np.arange(1, 301, dtype=float))  # 1..300
    # start = value 253 bars from the end (48), end = value 22 bars from the end (279)
    assert ind.momentum_12_1(close, 252, 21) == pytest.approx(279 / 48 - 1)
    assert ind.momentum_12_1(close.iloc[:200], 252, 21) is None


def test_sma_and_atr_simple_cases():
    s = pd.Series([1, 2, 3, 4, 5], dtype=float)
    assert ind.sma(s, 5).iloc[-1] == 3
    bars = pd.DataFrame({"open": 10.0, "high": [11.0] * 5, "low": [9.0] * 5, "close": [10.0] * 5})
    assert ind.atr(bars, 3).iloc[-1] == pytest.approx(2.0)


def test_rsi_extremes():
    assert ind.rsi(pd.Series(np.arange(1, 30, dtype=float)), 2).iloc[-1] == 100
    assert ind.rsi(pd.Series(np.arange(30, 1, -1, dtype=float)), 2).iloc[-1] == pytest.approx(0)


# ---- calendar ---------------------------------------------------------------------

def test_monthly_trade_day_detection():
    sessions = pd.bdate_range("2021-06-01", "2021-07-09")
    assert session_index_in_month(date(2021, 7, 1), sessions) == 0
    assert is_monthly_trade_day(date(2021, 7, 1), sessions, 0)
    assert is_monthly_trade_day(date(2021, 7, 2), sessions, 1)
    assert not is_monthly_trade_day(date(2021, 7, 2), sessions, 0)


# ---- momentum ---------------------------------------------------------------------

def test_momentum_buys_top5_above_sma(momentum):
    growth = {"A": 0.004, "B": 0.003, "C": 0.0025, "D": 0.002, "E": 0.0015, "F": 0.001, "G": -0.001}
    bars = {s: trend_bars(g) for s, g in growth.items()}
    sigs = momentum.generate(ctx_for(bars))
    assert {s.symbol for s in sigs if s.side is Side.BUY} == {"A", "B", "C", "D", "E"}
    assert all(s.stop_distance > 0 for s in sigs)
    assert sum(s.target_weight for s in sigs) == pytest.approx(1.0)


def test_momentum_lower_volatility_gets_more_weight(momentum):
    bars = {"CALM": trend_bars(0.003, noise=0.001), "WILD": trend_bars(0.003, noise=0.01)}
    sigs = {s.symbol: s for s in momentum.generate(ctx_for(bars))}
    assert sigs["CALM"].target_weight > sigs["WILD"].target_weight


def test_momentum_sells_holding_that_left_top5(momentum):
    growth = {"A": 0.004, "B": 0.003, "C": 0.0025, "D": 0.002, "E": 0.0015, "F": 0.001}
    bars = {s: trend_bars(g) for s, g in growth.items()}
    pos = {"F": Position("F", 10, 100.0, date(2021, 1, 4))}
    sigs = momentum.generate(ctx_for(bars, pos))
    sells = [s for s in sigs if s.side is Side.SELL]
    assert [s.symbol for s in sells] == ["F"] and "left top 5" in sells[0].reason


def test_momentum_skips_and_sells_top_stock_below_its_sma(momentum):
    # X had the best 12-1 return but crashed in the last (skipped) month, below its 200-day SMA.
    bars = {"X": trend_bars(0.006, crash=(15, 0.6)), "A": trend_bars(0.002), "B": trend_bars(0.001)}
    assert ind.momentum_12_1(bars["X"]["close"], 252, 21) > ind.momentum_12_1(bars["A"]["close"], 252, 21)
    sigs = momentum.generate(ctx_for(bars, {"X": Position("X", 10, 100.0, date(2021, 1, 4))}))
    assert "X" not in {s.symbol for s in sigs if s.side is Side.BUY}
    sell = [s for s in sigs if s.side is Side.SELL and s.symbol == "X"]
    assert sell and "200-day SMA" in sell[0].reason


def test_momentum_does_nothing_mid_month(momentum):
    bars = {"A": trend_bars(0.004, end="2021-06-14")}
    assert momentum.generate(ctx_for(bars, next_session=date(2021, 6, 15))) == []


def test_momentum_keeps_existing_holding_without_resizing(momentum):
    bars = {"A": trend_bars(0.004), "B": trend_bars(0.003)}
    sigs = momentum.generate(ctx_for(bars, {"A": Position("A", 10, 100.0, date(2021, 1, 4))}))
    assert [s.symbol for s in sigs] == ["B"]


# ---- faber ------------------------------------------------------------------------

@pytest.fixture
def faber(cfg):
    return build_strategy("faber", cfg.strategy_params("faber"))


def test_faber_buys_spy_above_10_month_sma(faber):
    bars = {"SPY": trend_bars(0.001)}
    sigs = faber.generate(ctx_for(bars))
    assert len(sigs) == 1 and sigs[0].side is Side.BUY and sigs[0].symbol == "SPY"


def test_faber_sells_spy_below_10_month_sma(faber):
    bars = {"SPY": trend_bars(-0.001)}
    sigs = faber.generate(ctx_for(bars, {"SPY": Position("SPY", 10, 100.0, date(2021, 1, 4))}))
    assert len(sigs) == 1 and sigs[0].side is Side.SELL


def test_faber_holds_when_already_in_and_trend_up(faber):
    bars = {"SPY": trend_bars(0.001)}
    assert faber.generate(ctx_for(bars, {"SPY": Position("SPY", 10, 100.0, date(2021, 1, 4))})) == []


def test_faber_only_trades_at_month_end(faber):
    bars = {"SPY": trend_bars(0.001, end="2021-06-14")}
    assert faber.generate(ctx_for(bars, next_session=date(2021, 6, 15))) == []


# ---- rsi2 -------------------------------------------------------------------------

@pytest.fixture
def rsi2(cfg):
    return build_strategy("rsi2", cfg.strategy_params("rsi2"))


def pullback_closes(n=260, drops=3):
    up = list(100 * np.exp(np.cumsum(np.full(n, 0.002))))
    for _ in range(drops):
        up.append(up[-1] * 0.985)
    return up


def test_rsi2_buys_pullback_in_uptrend(rsi2):
    bars = {"A": bars_from_closes(pullback_closes())}
    sigs = rsi2.generate(ctx_for(bars, next_session=date(2021, 1, 1)))
    assert len(sigs) == 1 and sigs[0].side is Side.BUY and sigs[0].inputs["rsi2"] < 10


def test_rsi2_no_buy_when_below_200_sma(rsi2):
    closes = list(100 * np.exp(np.cumsum(np.full(260, -0.002))))
    closes += [closes[-1] * 0.985] * 1 + [closes[-1] * 0.97]
    bars = {"A": bars_from_closes(closes)}
    assert rsi2.generate(ctx_for(bars, next_session=date(2021, 1, 1))) == []


def test_rsi2_exits_on_close_above_5_day_sma(rsi2):
    closes = pullback_closes() + [pullback_closes()[-1] * 1.05]
    bars = {"A": bars_from_closes(closes)}
    entry = bars["A"].index[-2].date()
    sigs = rsi2.generate(ctx_for(bars, {"A": Position("A", 10, 100.0, entry)}, next_session=date(2021, 1, 1)))
    assert len(sigs) == 1 and sigs[0].side is Side.SELL and "SMA" in sigs[0].reason


def test_rsi2_time_stop_after_5_sessions(rsi2):
    closes = pullback_closes(drops=8)  # keeps falling: never closes above the 5-day SMA
    bars = {"A": bars_from_closes(closes)}
    idx = bars["A"].index
    held4 = rsi2.generate(ctx_for(bars, {"A": Position("A", 10, 100.0, idx[-4].date())},
                                  next_session=date(2021, 1, 1)))
    held5 = rsi2.generate(ctx_for(bars, {"A": Position("A", 10, 100.0, idx[-5].date())},
                                  next_session=date(2021, 1, 1)))
    assert not [s for s in held4 if s.side is Side.SELL]
    assert [s.reason for s in held5 if s.side is Side.SELL] == ["time stop: held 5 sessions"]


# ---- market filter ----------------------------------------------------------------

def test_market_filter_blocks_buys_not_sells():
    spy_down = {"SPY": trend_bars(-0.001)}
    fr = market_filter.evaluate(spy_down, "SPY", 200)
    assert not fr.risk_on
    sigs = [Signal("A", Side.BUY, "x"), Signal("B", Side.SELL, "y")]
    kept, blocked = market_filter.apply(sigs, fr)
    assert [s.symbol for s in kept] == ["B"] and [s.symbol for s in blocked] == ["A"]
    assert market_filter.evaluate({"SPY": trend_bars(0.001)}, "SPY", 200).risk_on
