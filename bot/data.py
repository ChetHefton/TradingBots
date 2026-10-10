"""Daily bars: Alpaca historical data (cached to CSV) and a synthetic source
for tests and offline demos.

Bars are split- and dividend-adjusted, indexed by session date (tz-naive),
with columns open, high, low, close, volume.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

COLUMNS = ["open", "high", "low", "close", "volume"]


class AlpacaData:
    def __init__(self, api_key: str, secret_key: str, cache_dir: Path,
                 history_feed: str = "sip", latest_feed: str = "iex"):
        from alpaca.data.historical import StockHistoricalDataClient

        self.client = StockHistoricalDataClient(api_key, secret_key)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.history_feed = history_feed
        self.latest_feed = latest_feed

    def _cache_path(self, symbol: str) -> Path:
        return self.cache_dir / f"{symbol}_{self.history_feed}.csv"

    def get_daily_bars(self, symbols: list[str], start: date, end: date | None = None,
                       refresh: bool = False) -> dict[str, pd.DataFrame]:
        """Bars from `start` to `end` (inclusive). Free SIP history must end
        more than 15 minutes ago, so `end` defaults to yesterday."""
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = end or (datetime.now(timezone.utc).date() - timedelta(days=1))
        out: dict[str, pd.DataFrame] = {}
        to_fetch = []
        for sym in symbols:
            cp = self._cache_path(sym)
            if not refresh and cp.exists():
                df = pd.read_csv(cp, index_col=0, parse_dates=True)
                # Adjusted history changes whenever a split or dividend happens, so
                # never append to a cache: reuse it only if it already reaches `end`
                # or was downloaded in the last 12 hours, otherwise fetch it all again.
                fresh = (time.time() - cp.stat().st_mtime) < 12 * 3600
                if not df.empty and df.index[0].date() <= start + timedelta(days=7) \
                        and (df.index[-1].date() >= end or fresh):
                    out[sym] = df.loc[pd.Timestamp(start):pd.Timestamp(end)]
                    continue
            to_fetch.append(sym)

        if to_fetch:
            req = StockBarsRequest(
                symbol_or_symbols=to_fetch,
                timeframe=TimeFrame.Day,
                start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
                end=datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc),
                adjustment=Adjustment.ALL,
                feed=DataFeed(self.history_feed),
            )
            raw = self.client.get_stock_bars(req).df
            for sym in to_fetch:
                if raw.empty or sym not in raw.index.get_level_values(0):
                    out[sym] = pd.DataFrame(columns=COLUMNS)
                    continue
                df = raw.xs(sym, level=0)[COLUMNS].copy()
                # Alpaca stamps daily bars at midnight New York time; use the date.
                idx = pd.DatetimeIndex(df.index)
                if idx.tz is not None:
                    idx = idx.tz_convert("America/New_York").tz_localize(None)
                df.index = idx.normalize()
                df = df[~df.index.duplicated(keep="last")].sort_index()
                df.to_csv(self._cache_path(sym))
                out[sym] = df
        return out

    def get_latest_price(self, symbol: str) -> float | None:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=DataFeed(self.latest_feed))
        trade = self.client.get_stock_latest_trade(req).get(symbol)
        return float(trade.price) if trade else None


def synthetic_bars(symbols: list[str], start: date, end: date, seed: int = 7) -> dict[str, pd.DataFrame]:
    """Random-walk bars for tests and offline demos. Results on this data mean
    nothing about real markets."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    out = {}
    for i, sym in enumerate(symbols):
        drift = rng.normal(0.0003, 0.0003)
        vol = rng.uniform(0.008, 0.025)
        rets = rng.normal(drift, vol, len(days))
        close = 100 * np.exp(np.cumsum(rets))
        open_ = close * np.exp(rng.normal(0, vol / 3, len(days)))
        high = np.maximum(open_, close) * np.exp(np.abs(rng.normal(0, vol / 2, len(days))))
        low = np.minimum(open_, close) * np.exp(-np.abs(rng.normal(0, vol / 2, len(days))))
        out[sym] = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                                 "volume": 1_000_000}, index=days)
    return out
