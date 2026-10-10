"""Strategy 2 (comparison): Faber's 10-month trend filter on SPY.

At each month end, hold SPY if its month-end close is above the average of the
last 10 month-end closes; otherwise hold cash. Trades at the next open.
"""
from __future__ import annotations

from bot import indicators as ind
from bot.models import Side, Signal
from bot.strategies.base import Strategy, StrategyContext, is_monthly_trade_day


class FaberStrategy(Strategy):
    name = "faber"

    def warmup_days(self) -> int:
        return 21 * (self.params["sma_months"] + 1)

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        sym = p["symbol"]
        # Decide at the close of the month's last session (next session is the
        # first trading day of a new month).
        if not is_monthly_trade_day(ctx.next_session, ctx.sessions, 0):
            return []
        bars = ctx.bars.get(sym)
        if bars is None or bars.empty:
            return []
        monthly = bars["close"].groupby([bars.index.year, bars.index.month]).last()
        if len(monthly) < p["sma_months"]:
            return []
        sma_m = float(monthly.iloc[-p["sma_months"]:].mean())
        close = float(bars["close"].iloc[-1])
        inputs = {"month_end_close": close, f"sma_{p['sma_months']}m": round(sma_m, 4)}

        held = sym in ctx.positions
        if close > sma_m and not held:
            atr_v = ind.last(ind.atr(bars, p["atr_days"]))
            if not atr_v:
                return []
            return [Signal(sym, Side.BUY, f"month-end close above {p['sma_months']}-month SMA",
                           target_weight=1.0, stop_distance=p["atr_stop_mult"] * atr_v,
                           inputs={**inputs, "atr": round(atr_v, 4)})]
        if close <= sma_m and held:
            return [Signal(sym, Side.SELL, f"month-end close at/below {p['sma_months']}-month SMA",
                           inputs=inputs)]
        return []
