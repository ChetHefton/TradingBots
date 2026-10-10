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
