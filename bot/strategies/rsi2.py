"""Strategy 3 (comparison): Connors-style RSI-2 pullback.

Buy at the next open when the 2-day RSI closes below 10 and the close is above
the 200-day SMA. Sell at the next open after a close above the 5-day SMA, or
once the position has been held for 5 sessions. Hard stop at 2x ATR.
"""
from __future__ import annotations

import pandas as pd

from bot import indicators as ind
from bot.models import Side, Signal
from bot.strategies.base import Strategy, StrategyContext


class RSI2Strategy(Strategy):
    name = "rsi2"

    def warmup_days(self) -> int:
        p = self.params
        return max(p["trend_sma_days"], p["atr_days"] + 1, p["rsi_days"] * 10)

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        signals: list[Signal] = []

        for sym, pos in sorted(ctx.positions.items()):
            bars = ctx.bars.get(sym)
            if bars is None or bars.empty:
                continue
            close = bars["close"]
            sma5 = ind.last(ind.sma(close, p["exit_sma_days"]))
            last_close = float(close.iloc[-1])
            held = int((close.index >= pd.Timestamp(pos.entry_date)).sum())
            if sma5 is not None and last_close > sma5:
                signals.append(Signal(sym, Side.SELL, f"close above {p['exit_sma_days']}-day SMA",
                                      inputs={"close": last_close, "sma5": round(sma5, 4)}))
            elif held >= p["max_hold_days"]:
                signals.append(Signal(sym, Side.SELL, f"time stop: held {held} sessions",
                                      inputs={"sessions_held": held}))

        for sym in ctx.universe:
            if sym in ctx.positions:
                continue
            bars = ctx.bars.get(sym)
            if bars is None or len(bars) < self.warmup_days():
                continue
            close = bars["close"]
            r = ind.last(ind.rsi(close, p["rsi_days"]))
            sma200 = ind.last(ind.sma(close, p["trend_sma_days"]))
            last_close = float(close.iloc[-1])
            if r is None or sma200 is None:
                continue
            if r < p["entry_below"] and last_close > sma200:
                atr_v = ind.last(ind.atr(bars, p["atr_days"]))
                if not atr_v:
                    continue
                signals.append(Signal(
                    sym, Side.BUY, f"RSI({p['rsi_days']}) {r:.1f} < {p['entry_below']} above 200-day SMA",
                    target_weight=1.0, stop_distance=p["atr_stop_mult"] * atr_v,
                    inputs={"rsi2": round(r, 2), "close": last_close, "sma200": round(sma200, 4),
                            "atr14": round(atr_v, 4)},
                ))
        return signals
