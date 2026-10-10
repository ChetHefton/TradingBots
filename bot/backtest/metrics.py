"""Performance metrics for a backtest result."""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from bot.backtest.engine import BacktestResult

TRADING_DAYS = 252


@dataclass
class Metrics:
    name: str
    total_return: float
    cagr: float
    max_drawdown: float
    sharpe: float
    win_rate: float | None
    trades: int
    time_in_market: float
    kill_switch_date: str | None

    def as_row(self) -> dict[str, str]:
        def pct(x: float | None) -> str:
            return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.1%}"
        return {
            "Strategy": self.name,
            "Total return": pct(self.total_return),
            "CAGR": pct(self.cagr),
            "Max drawdown": pct(self.max_drawdown),
            "Sharpe": "n/a" if math.isnan(self.sharpe) else f"{self.sharpe:.2f}",
            "Win rate": pct(self.win_rate),
            "Trades": str(self.trades),
            "Time in market": pct(self.time_in_market),
            "Kill switch": self.kill_switch_date or "-",
        }


def max_drawdown(equity: pd.Series) -> float:
    peak = equity.cummax()
    return float((equity / peak - 1).min())


def compute(result: BacktestResult) -> Metrics:
    # Start the curve from the starting capital, the day before the first session.
    start = pd.Series([result.initial_capital], index=[result.equity.index[0] - pd.Timedelta(days=1)])
    eq = pd.concat([start, result.equity])
    total = float(eq.iloc[-1] / eq.iloc[0] - 1)
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1 / 365.25)
    cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
    rets = eq.pct_change().dropna()
    sd = rets.std(ddof=1)
    sharpe = float(rets.mean() / sd * math.sqrt(TRADING_DAYS)) if sd and sd > 0 else float("nan")
    wins = [t for t in result.trades if t.pnl > 0]
    win_rate = len(wins) / len(result.trades) if result.trades else None
    kill = next((d.isoformat() for d, kind, _ in result.events if kind == "kill_switch"), None)
    return Metrics(
        name=result.name,
        total_return=total,
        cagr=cagr,
        max_drawdown=max_drawdown(eq),
        sharpe=sharpe,
        win_rate=win_rate,
        trades=result.entries,
        time_in_market=float(result.in_market.mean()) if len(result.in_market) else 0.0,
        kill_switch_date=kill,
    )
