"""Data cache: never serve stale bars to the paper loop."""
import os
import time
from datetime import date
from types import SimpleNamespace

import pandas as pd

from bot.data import AlpacaData


class StubClient:
    def __init__(self, last_day: str):
        self.calls = 0
        self.last_day = last_day

    def get_stock_bars(self, req):
        self.calls += 1
        rows = []
        for sym in req.symbol_or_symbols:
            for ts in pd.bdate_range("2026-01-02", self.last_day, tz="America/New_York"):
                rows.append((sym, ts, 1.0, 1.0, 1.0, 1.0, 100))
        df = pd.DataFrame(rows, columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"])
        return SimpleNamespace(df=df.set_index(["symbol", "timestamp"]))


def make(tmp_path, client):
    d = AlpacaData.__new__(AlpacaData)
    d.client, d.cache_dir, d.history_feed, d.latest_feed = client, tmp_path, "sip", "iex"
    return d


def test_bars_are_indexed_by_new_york_date(tmp_path):
    d = make(tmp_path, StubClient("2026-03-31"))
    bars = d.get_daily_bars(["SPY"], date(2026, 1, 2), date(2026, 3, 31))["SPY"]
    assert bars.index[0] == pd.Timestamp("2026-01-02") and bars.index.tz is None


def test_complete_cache_is_reused(tmp_path):
    client = StubClient("2026-03-31")
    d = make(tmp_path, client)
    d.get_daily_bars(["SPY"], date(2026, 1, 2), date(2026, 3, 31))
    d.get_daily_bars(["SPY"], date(2026, 1, 2), date(2026, 3, 31))
    assert client.calls == 1


def test_old_cache_missing_recent_days_is_refetched(tmp_path):
    client = StubClient("2026-03-27")
    d = make(tmp_path, client)
    d.get_daily_bars(["SPY"], date(2026, 1, 2), date(2026, 3, 27))
    cache = tmp_path / "SPY_sip.csv"
    old = time.time() - 24 * 3600
    os.utime(cache, (old, old))
    client.last_day = "2026-03-31"
    bars = d.get_daily_bars(["SPY"], date(2026, 1, 2), date(2026, 3, 31))["SPY"]
    assert client.calls == 2 and bars.index[-1] == pd.Timestamp("2026-03-31")
