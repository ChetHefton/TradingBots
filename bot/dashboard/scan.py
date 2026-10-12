"""What the bot sees right now, per symbol: trend, momentum rank, stop
distance and RSI-2, computed with the same indicator code and parameters as
the strategies. Display only; the paper loop does its own calculation."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from bot import indicators as ind
from bot import market_filter
from bot.config import Config

CHART_DAYS = 260  # about a year of sessions


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), nd)


def next_monthly_trade_day(today: date, offset: int = 0) -> date:
    """First (+offset) weekday of next month. Ignores exchange holidays, so
    it is approximate around New Year's Day."""
    start = (pd.Timestamp(today) + pd.offsets.MonthBegin(1)).normalize()
    return pd.bdate_range(start, periods=offset + 1)[-1].date()


def scan(cfg: Config, bars: dict[str, pd.DataFrame], today: date | None = None) -> dict[str, Any]:
    mom = cfg.strategy_params("momentum")
    rsi_p = cfg.strategy_params("rsi2")
    mf = cfg.market_filter
    strategy = cfg.default_strategy
    fr = market_filter.evaluate(bars, mf["symbol"], mf["sma_days"])

    rows: list[dict[str, Any]] = []
    for sym in cfg.symbols:
        df = bars.get(sym)
        if df is None or len(df) < 2:
            rows.append({"symbol": sym, "available": False})
            continue
        close = df["close"]
        sma_s = ind.sma(close, mom["trend_sma_days"])
        sma = ind.last(sma_s)
        last = float(close.iloc[-1])
        atr = ind.last(ind.atr(df, mom["atr_days"]))
        tail = df.iloc[-CHART_DAYS:]
        rows.append({
            "symbol": sym,
            "available": True,
            "as_of": df.index[-1].strftime("%Y-%m-%d"),
            "close": _r(last, 2),
            "change_1d": _r(close.iloc[-1] / close.iloc[-2] - 1),
            "sma200": _r(sma, 2),
            "above_trend": sma is not None and last > sma,
            "pct_vs_sma200": _r(last / sma - 1) if sma else None,
            "momentum_12_1": _r(ind.momentum_12_1(close, mom["lookback_days"], mom["skip_days"])),
            "vol20": _r(ind.volatility(close, mom["vol_days"])),
            "atr14": _r(atr, 2),
            "stop_if_bought": _r(last - mom["atr_stop_mult"] * atr, 2) if atr else None,
            "rsi2": _r(ind.last(ind.rsi(close, rsi_p["rsi_days"])), 1),
            "history_days": len(df),
            "series": {
                "dates": tail.index.strftime("%Y-%m-%d").tolist(),
                "close": [round(float(v), 2) for v in tail["close"]],
                "sma200": [None if pd.isna(v) else round(float(v), 2) for v in sma_s.iloc[-CHART_DAYS:]],
            },
        })

    ranked = sorted((r for r in rows if r.get("momentum_12_1") is not None),
                    key=lambda r: (-r["momentum_12_1"], r["symbol"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    top_n = mom["top_n"]
    for r in rows:
        if not r["available"]:
            r["momentum_status"] = r["rsi2_status"] = "NO DATA"
            r["status"] = "NO DATA"
            continue
        if r.get("rank") is None:
            r["momentum_status"] = "NEEDS 1Y HISTORY"
        elif not r["above_trend"]:
            r["momentum_status"] = "BELOW TREND"
        elif r["rank"] <= top_n:
            r["momentum_status"] = "BUY CANDIDATE" if fr.risk_on else "CANDIDATE · FILTER OFF"
        else:
            r["momentum_status"] = f"RANK {r['rank']}"

        r["rsi2_signal"] = bool(r["above_trend"] and r.get("rsi2") is not None
                                and r["rsi2"] < rsi_p["entry_below"])
        if r["sma200"] is None:
            r["rsi2_status"] = "NEEDS HISTORY"
        elif not r["above_trend"]:
            r["rsi2_status"] = "BELOW TREND"
        elif r["rsi2_signal"]:
            r["rsi2_status"] = "BUY SIGNAL" if fr.risk_on else "SIGNAL · FILTER OFF"
        else:
            r["rsi2_status"] = f"WAIT · RSI {r['rsi2']:.0f}" if r.get("rsi2") is not None else "WAIT"
        r["status"] = r["rsi2_status"] if strategy == "rsi2" else r["momentum_status"]

    today = today or date.today()
    return {
        "strategy": strategy,
        "rsi2_entry_below": rsi_p["entry_below"],
        "rsi2_max_hold_days": rsi_p["max_hold_days"],
        "market": {"symbol": mf["symbol"], "sma_days": mf["sma_days"], "risk_on": fr.risk_on,
                   "close": _r(fr.close, 2), "sma": _r(fr.sma, 2)},
        "top_n": top_n,
        "next_rebalance": next_monthly_trade_day(today, int(mom.get("rebalance_offset", 0))).isoformat(),
        "rows": rows,
    }
