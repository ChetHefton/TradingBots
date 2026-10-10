"""Paper loop behaviour against a fake broker."""
from datetime import date

import pytest

from bot.broker import BrokerPosition
from bot.state import BotState, load_state
from bot.strategies import build_strategy
from bot.trader import Trader
from tests.conftest import FakeBroker, FakeData, make_session, mid_session
from tests.test_strategies import trend_bars

FIRST_OF_MONTH = date(2026, 10, 1)  # Thursday, first session of October
MID_MONTH = date(2026, 10, 14)
NO_SLEEP = lambda s: None  # noqa: E731


def market_data(end="2026-09-30"):
    growth = {"SPY": 0.0008, "AAPL": 0.004, "MSFT": 0.003, "NVDA": 0.0025, "JPM": 0.002, "XOM": 0.0015,
              "PG": 0.001, "JNJ": -0.001}
    return {s: trend_bars(g, end=end) for s, g in growth.items()}


def make_trader(cfg, tmp_path, broker, data=None, dry_run=False, strategy="momentum", state=None):
    cfg.raw["run_loop"]["fill_wait_seconds"] = 0
    data = data or FakeData(market_data(), latest={s: 100.0 for s in cfg.symbols})
    return Trader(cfg, broker, data, build_strategy(strategy, cfg.strategy_params(strategy)),
                  state or BotState(), tmp_path / "state.json", tmp_path / "trades.csv",
                  dry_run=dry_run, sleep=NO_SLEEP)


def test_unexpected_position_is_adopted_and_protected(cfg, tmp_path, caplog):
    broker = FakeBroker(positions={"AAPL": BrokerPosition("AAPL", 10, 150.0)})
    t = make_trader(cfg, tmp_path, broker)
    positions = t.reconcile(MID_MONTH)
    assert "AAPL" in positions and t.state.positions["AAPL"].strategy == "unknown"
    t.protect_positions(positions, MID_MONTH)
    stops = [o for o in broker.get_open_orders() if o.symbol == "AAPL" and o.type == "stop"]
    assert len(stops) == 1 and stops[0].qty == 10 and 0 < stops[0].stop_price


def test_position_outside_allowlist_is_left_alone(cfg, tmp_path):
    broker = FakeBroker(positions={"GME": BrokerPosition("GME", 10, 20.0)})
    t = make_trader(cfg, tmp_path, broker)
    positions = t.reconcile(MID_MONTH)
    t.protect_positions(positions, MID_MONTH)
    assert positions == {} and broker.submissions == []


def test_position_that_vanished_is_logged_and_forgotten(cfg, tmp_path):
    broker = FakeBroker()
    t = make_trader(cfg, tmp_path, broker)
    from bot.state import PositionMeta
    t.state.positions["MSFT"] = PositionMeta("2026-09-01", "momentum", 90.0, "lot1", 100.0)
    broker._add("stop-1", "MSFT", "sell", "stop", 5, "filled", stop=90.0, price=89.5)
    assert t.reconcile(MID_MONTH) == {}
    assert "MSFT" not in t.state.positions
    assert "MSFT" in (tmp_path / "trades.csv").read_text()


def test_full_cycle_buys_with_stops_once(cfg, tmp_path):
    broker = FakeBroker()
    t = make_trader(cfg, tmp_path, broker)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    bought = set(broker.positions)
    assert bought == {"AAPL", "MSFT", "NVDA", "JPM", "XOM"}
    for sym in bought:
        assert any(o.symbol == sym and o.type == "stop" for o in broker.get_open_orders())
        assert broker.positions[sym].qty * 100.0 <= cfg.risk.max_position_pct * 100_000 + 1e-6
    n = len(broker.submissions)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    assert len(broker.submissions) == n  # same day: no second cycle
    assert load_state(tmp_path / "state.json").last_cycle_day == FIRST_OF_MONTH.isoformat()


def test_restart_with_lost_state_does_not_double_submit(cfg, tmp_path):
    broker = FakeBroker()
    make_trader(cfg, tmp_path, broker).run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    entries = [c for c in broker.submissions if c.endswith("-entry")]
    # The bot crashes after submitting, before its state is saved, and the broker
    # hasn't reported the positions yet. The restarted bot generates the same
    # buys; the deterministic client_order_ids must stop them going out twice.
    broker.positions.clear()
    t2 = make_trader(cfg, tmp_path / "fresh", broker)
    t2.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    assert [c for c in broker.submissions if c.endswith("-entry")] == entries


def test_dry_run_places_no_orders(cfg, tmp_path):
    broker = FakeBroker(positions={"PG": BrokerPosition("PG", 10, 100.0)})
    t = make_trader(cfg, tmp_path, broker, dry_run=True)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    assert broker.submissions == [] and broker.cancelled == []
    assert t.risk.state.orders_today == 0


def test_market_closed_places_no_orders(cfg, tmp_path):
    broker = FakeBroker()
    t = make_trader(cfg, tmp_path, broker)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), None)
    assert broker.submissions == [] and t.state.last_cycle_day is None


def test_daily_loss_halt_cancels_entries_keeps_stops(cfg, tmp_path):
    broker = FakeBroker(equity=97_000, last_equity=100_000,
                        positions={"AAPL": BrokerPosition("AAPL", 10, 100.0)})
    broker._add("pending-buy", "MSFT", "buy", "market", 5, "new")
    broker._add("aapl-stop", "AAPL", "sell", "stop", 10, "new", stop=90.0)
    t = make_trader(cfg, tmp_path, broker)
    t.state.positions["AAPL"] = __import__("bot.state", fromlist=["PositionMeta"]).PositionMeta(
        "2026-09-01", "momentum", 90.0, "lot", 100.0)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    assert broker.cancelled == ["pending-buy"]
    assert broker.orders["aapl-stop"].status == "new"
    assert t.risk.halted_today
    assert [c for c in broker.submissions if c.endswith("-entry")] == []


def test_kill_switch_blocks_cycle(cfg, tmp_path):
    state = BotState()
    state.risk.kill_switch = True
    state.risk.kill_reason = "test"
    broker = FakeBroker()
    t = make_trader(cfg, tmp_path, broker, state=state)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    assert broker.submissions == []


def test_monthly_rebalance_sells_dropouts(cfg, tmp_path):
    broker = FakeBroker(positions={"JNJ": BrokerPosition("JNJ", 10, 100.0)})
    t = make_trader(cfg, tmp_path, broker)
    t.run_daily_cycle(mid_session(FIRST_OF_MONTH), make_session(FIRST_OF_MONTH))
    assert "JNJ" not in broker.positions
    assert any(c.endswith("JNJ-sell-exit") for c in broker.submissions)
