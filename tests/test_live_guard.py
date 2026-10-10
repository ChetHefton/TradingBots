"""The live trading endpoint must be unreachable from this code."""
from pathlib import Path

import pytest
import yaml

from bot.broker import PAPER_TRADING_URL, AlpacaPaperBroker, assert_paper_client
from bot.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, LiveTradingDisabledError, load_config

LIVE_HOST = "api.alpaca.markets"


def test_config_refuses_live_flag(tmp_path):
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["mode"]["live_trading"] = True
    p = tmp_path / "settings.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(LiveTradingDisabledError):
        load_config(p)


def test_override_cannot_enable_live():
    with pytest.raises(LiveTradingDisabledError):
        load_config(overrides=["mode.live_trading=true"])


def test_broker_refuses_live_flag_even_if_config_was_tampered(cfg):
    cfg.raw["mode"]["live_trading"] = True
    with pytest.raises(LiveTradingDisabledError):
        AlpacaPaperBroker(cfg, "key", "secret")


def test_broker_client_points_at_paper_endpoint(cfg):
    broker = AlpacaPaperBroker(cfg, "key", "secret")  # constructing makes no network call
    base = getattr(broker.client._base_url, "value", broker.client._base_url)
    assert str(base) == PAPER_TRADING_URL


def test_assert_paper_client_rejects_live_client():
    from alpaca.trading.client import TradingClient

    live = TradingClient("key", "secret", paper=False)
    with pytest.raises(LiveTradingDisabledError):
        assert_paper_client(live)


def test_no_live_url_or_paper_false_anywhere_in_source():
    for path in list((PROJECT_ROOT / "bot").rglob("*.py")) + list((PROJECT_ROOT / "scripts").rglob("*.py")):
        text = path.read_text()
        for line in text.splitlines():
            stripped = line.replace("paper-api.alpaca.markets", "")
            assert f"https://{LIVE_HOST}" not in stripped, f"live URL in {path}"
            assert "paper=False" not in line, f"paper=False in {path}"
            assert "url_override" not in line or line.strip().startswith("#"), f"url_override in {path}"


def test_requests_to_live_host_are_blocked(cfg, monkeypatch):
    """Belt and braces: route every HTTP call through a check, then exercise
    the broker. Any request to the live host fails the test."""
    import requests

    seen = []

    def fake_request(self, method, url, *a, **k):
        seen.append(url)
        assert "://api.alpaca.markets" not in url
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "request", fake_request)
    broker = AlpacaPaperBroker(cfg, "key", "secret")
    with pytest.raises(Exception):
        broker.get_account()
    assert seen and all(u.startswith(PAPER_TRADING_URL) for u in seen)
