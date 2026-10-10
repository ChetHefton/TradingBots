"""Strategy 1: 12-1 momentum with a trend filter (the default paper strategy).

Once a month: rank the universe by 12-month return excluding the latest
month, take the top N, and hold those that are also above their own 200-day
SMA, weighted by inverse 20-day volatility. Sell a holding at rebalance when it
leaves that set. Hard stops (2x ATR) are placed by the execution layer using
`stop_distance`.
"""
from __future__ import annotations

from bot import indicators as ind
from bot.models import Side, Signal
from bot.strategies.base import Strategy, StrategyContext, is_monthly_trade_day


class MomentumStrategy(Strategy):
    name = "momentum"

    def warmup_days(self) -> int:
        p = self.params
        return max(p["lookback_days"] + 1, p["trend_sma_days"], p["atr_days"] + 1, p["vol_days"] + 1)

    def rank(self, ctx: StrategyContext) -> list[tuple[str, float]]:
        p = self.params
        scores = []
        for sym in ctx.universe:
            bars = ctx.bars.get(sym)
            if bars is None or bars.empty:
                continue
            m = ind.momentum_12_1(bars["close"], p["lookback_days"], p["skip_days"])
            if m is not None:
                scores.append((sym, m))
        # Sort by score, then symbol, so ties are deterministic.
        scores.sort(key=lambda x: (-x[1], x[0]))
        return scores

    def generate(self, ctx: StrategyContext) -> list[Signal]:
        p = self.params
        if not is_monthly_trade_day(ctx.next_session, ctx.sessions, p.get("rebalance_offset", 0)):
            return []

        ranking = self.rank(ctx)
        top = ranking[: p["top_n"]]
        rank_of = {s: i + 1 for i, (s, _) in enumerate(ranking)}

        targets: dict[str, dict] = {}
        for sym, score in top:
            close = ctx.bars[sym]["close"]
            sma200 = ind.last(ind.sma(close, p["trend_sma_days"]))
            last_close = float(close.iloc[-1])
            if sma200 is None or last_close <= sma200:
                continue
            vol = ind.volatility(close, p["vol_days"])
            atr_v = ind.last(ind.atr(ctx.bars[sym], p["atr_days"]))
            if not vol or not atr_v:
                continue
            targets[sym] = dict(momentum=round(score, 4), rank=rank_of[sym], close=last_close,
                                sma200=round(sma200, 4), vol20=round(vol, 5), atr14=round(atr_v, 4))

        signals: list[Signal] = []
        for sym in sorted(ctx.positions):
            if sym in targets:
                continue
            if sym in rank_of and rank_of[sym] <= p["top_n"]:
                reason = "closed below 200-day SMA at rebalance"
            else:
                reason = f"left top {p['top_n']} at rebalance (rank {rank_of.get(sym, 'n/a')})"
            signals.append(Signal(sym, Side.SELL, reason, inputs={"rank": rank_of.get(sym)}))

        inv_vol = {s: 1 / t["vol20"] for s, t in targets.items()}
        total = sum(inv_vol.values())
        for sym, t in targets.items():
            if sym in ctx.positions:
                continue  # keep existing holdings as they are; no resizing churn
            weight = inv_vol[sym] / total
            signals.append(Signal(
                sym, Side.BUY,
                reason=f"rank {t['rank']} by 12-1 momentum and above 200-day SMA",
                target_weight=weight,
                stop_distance=p["atr_stop_mult"] * t["atr14"],
                inputs={**t, "inv_vol_weight": round(weight, 4)},
            ))
        return signals
