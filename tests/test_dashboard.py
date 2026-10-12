"""Dashboard: read-only, localhost-only, serves data from the bot's files."""
import json
import threading
import urllib.error
import urllib.request

import pytest
import yaml

from bot.config import DEFAULT_CONFIG_PATH, load_config
from bot.dashboard.server import DashboardData, make_server
from bot.state import BotState, save_state


@pytest.fixture
def served(tmp_path):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["paths"] = {"log_dir": str(tmp_path / "logs"), "state_file": str(tmp_path / "state.json"),
                    "trade_log": str(tmp_path / "trades.csv")}
    raw["backtest"]["report_dir"] = str(tmp_path / "reports")
    (tmp_path / "settings.yaml").write_text(yaml.safe_dump(raw))
    cfg = load_config(tmp_path / "settings.yaml")

    state = BotState()
    state.risk.kill_switch = True
    state.risk.kill_reason = "drawdown 10% from peak"
    save_state(state, cfg.path("state_file"))
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "decisions.jsonl").write_text(
        "\n".join(json.dumps({"ts": f"t{i}", "level": "INFO", "event": f"e{i}"}) for i in range(5)) + "\n")
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "meta.json").write_text(json.dumps({"synthetic": True}))
    (tmp_path / "reports" / "equity_curves.csv").write_text(
        "period,strategy,date,index\ntest,momentum,2022-01-03,100.0\ntest,momentum,2022-01-04,101.5\n")

    calls = []

    def account():
        calls.append(1)
        return {"equity": 100_000.0, "cash": 50_000.0, "last_equity": 99_000.0, "positions": []}

    server = make_server(DashboardData(cfg, account), 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", server, calls
    server.shutdown()
    server.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read()


def test_binds_to_localhost_only(served):
    _, server, _ = served
    assert server.server_address[0] == "127.0.0.1"


def test_page_and_assets_served(served):
    base, _, _ = served
    for path in ("/", "/app.js", "/style.css"):
        status, body = get(base + path)
        assert status == 200 and body


def test_summary_reports_state_and_account(served):
    base, _, calls = served
    data = json.loads(get(base + "/api/summary")[1])
    assert data["mode"] == "paper"
    assert data["state"]["risk"]["kill_switch"] is True
    assert data["account"]["available"] and data["account"]["equity"] == 100_000.0
    get(base + "/api/summary")
    assert len(calls) == 1  # account snapshot is cached


def test_backtest_and_decisions(served):
    base, _, _ = served
    bt = json.loads(get(base + "/api/backtest")[1])
    assert bt["available"] and bt["curves"]["test"]["momentum"][-1] == ["2022-01-04", 101.5]
    dec = json.loads(get(base + "/api/decisions?limit=3")[1])
    assert [d["event"] for d in dec] == ["e4", "e3", "e2"]  # newest first
    assert json.loads(get(base + "/api/trades")[1]) == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_are_refused(served, method):
    base, _, _ = served
    req = urllib.request.Request(base + "/api/summary", data=b"{}", method=method)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 405


def test_no_path_traversal(served):
    base, _, _ = served
    for path in ("/../config.py", "/static/../server.py", "/%2e%2e/settings.yaml"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(base + path)
        assert e.value.code == 404


def test_account_failure_does_not_break_dashboard(tmp_path):
    cfg = load_config()
    def boom():
        raise ConnectionError("offline")
    snap = DashboardData(cfg, boom).account()
    assert snap["available"] is False and "offline" in snap["reason"]


# ---- scan -------------------------------------------------------------------------

def test_scan_ranks_and_flags_like_the_strategy(cfg):
    from datetime import date
    from bot.dashboard.scan import next_monthly_trade_day, scan
    from tests.test_strategies import trend_bars

    bars = {"SPY": trend_bars(0.0008), "AAPL": trend_bars(0.004), "MSFT": trend_bars(0.003),
            "JNJ": trend_bars(-0.002), "XOM": trend_bars(0.006, crash=(15, 0.6))}
    out = scan(cfg, bars, today=date(2026, 10, 12))
    rows = {r["symbol"]: r for r in out["rows"]}
    assert out["market"]["risk_on"] is True
    assert rows["XOM"]["rank"] == 1 and rows["XOM"]["momentum_status"] == "BELOW TREND"  # best momentum, broken trend
    assert rows["AAPL"]["momentum_status"] == "BUY CANDIDATE"
    assert rows["JNJ"]["momentum_status"] == "BELOW TREND" and rows["JNJ"]["rsi2_status"] == "BELOW TREND"
    assert rows["AAPL"]["rsi2_status"].startswith("WAIT")  # steady uptrend: not oversold
    assert rows["AAPL"]["status"] == rows["AAPL"][f"{cfg.default_strategy}_status"]
    assert rows["QQQ"]["status"] == "NO DATA"
    assert rows["AAPL"]["stop_if_bought"] < rows["AAPL"]["close"]
    assert len(rows["AAPL"]["series"]["close"]) == 260
    assert out["next_rebalance"] == "2026-11-02"
    assert next_monthly_trade_day(date(2026, 1, 15), 2).isoformat() == "2026-02-04"


def test_scan_endpoint_without_price_data(served):
    base, _, _ = served
    data = json.loads(get(base + "/api/scan")[1])
    assert data["available"] is False and "price data" in data["reason"]


def test_scan_flags_rsi2_dip_in_uptrend(cfg):
    from bot.dashboard.scan import scan
    from tests.conftest import bars_from_closes
    from tests.test_strategies import pullback_closes, trend_bars

    dip = bars_from_closes(pullback_closes())
    spy = trend_bars(0.0008, n=len(dip))
    spy.index = dip.index
    row = next(r for r in scan(cfg, {"SPY": spy, "AAPL": dip})["rows"] if r["symbol"] == "AAPL")
    assert row["rsi2_signal"] and row["rsi2_status"] == "BUY SIGNAL"
