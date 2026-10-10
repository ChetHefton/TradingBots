from datetime import date, timedelta

import pytest

from bot.models import AccountView, Position
from bot.risk import RiskEngine, RiskState
from tests.conftest import make_session, mid_session

DAY = date(2026, 10, 5)


@pytest.fixture
def engine(cfg):
    e = RiskEngine(cfg.risk, cfg.symbols, cost_per_side=cfg.costs.per_side)
    e.start_day(DAY, 100_000)
    return e


def acct(equity=100_000.0, cash=100_000.0, positions=None):
    return AccountView(equity, cash, positions or {})


def entry(engine, symbol="AAPL", price=100.0, stop=95.0, weight=1.0, account=None, now=None, session="default"):
    session = make_session(DAY) if session == "default" else session
    return engine.check_entry(symbol, price, stop, weight, account or acct(), now or mid_session(DAY), session)


# ---- sizing ---------------------------------------------------------------------

def test_risk_per_trade_sizes_from_stop_distance(engine):
    # 1% of 100k = $1,000 risk; $10/share stop distance -> 100 shares ($5,000 < 10% cap).
    d = entry(engine, price=50.0, stop=40.0)
    assert d.approved and d.qty == 100


def test_position_cap_limits_size(engine):
    # Risk sizing allows 1000 shares ($1 stop), but 10% cap is $10,000 -> 100 shares.
    d = entry(engine, price=100.0, stop=99.0)
    assert d.approved and d.qty == 100
    assert d.checks["risk_qty"] == 1000 and d.checks["cap_qty"] == 100


def test_position_cap_exactly_hit(engine):
    # Exactly 10% of equity is allowed.
    d = entry(engine, price=1000.0, stop=900.0)
    assert d.approved and d.qty == 10 and d.qty * 1000.0 == 0.10 * 100_000


def test_target_weight_limits_size(engine):
    d = entry(engine, price=100.0, stop=90.0, weight=0.05)
    assert d.qty == 50


def test_cash_limits_size_no_margin(engine):
    d = entry(engine, price=100.0, stop=90.0, account=acct(cash=2_000.0))
    assert d.approved and d.qty == 19  # 2000 / (100 * 1.0) minus costs rounding


def test_zero_size_rejected(engine):
    d = entry(engine, price=100.0, stop=90.0, account=acct(cash=50.0))
    assert not d.approved and "zero" in d.reasons[-1]


def test_stop_must_be_below_price(engine):
    assert not entry(engine, price=100.0, stop=100.0).approved
    assert not entry(engine, price=100.0, stop=101.0).approved
    assert not entry(engine, price=100.0, stop=0.0).approved


def test_symbol_not_in_allowlist(engine):
    d = entry(engine, symbol="GME")
    assert not d.approved and "allowlist" in d.reasons[-1]


def test_no_adding_to_existing_position(engine):
    pos = {"AAPL": Position("AAPL", 10, 100.0, DAY)}
    assert not entry(engine, account=acct(positions=pos)).approved


# ---- exits / no shorting ----------------------------------------------------------

def test_exit_never_sells_more_than_held(engine):
    pos = {"AAPL": Position("AAPL", 10, 100.0, DAY)}
    d = engine.check_exit("AAPL", 50, acct(positions=pos), mid_session(DAY), make_session(DAY))
    assert d.approved and d.qty == 10


def test_exit_without_position_rejected_no_shorting(engine):
    d = engine.check_exit("AAPL", 10, acct(), mid_session(DAY), make_session(DAY))
    assert not d.approved and "no long position" in d.reasons[-1]


# ---- daily loss limit -------------------------------------------------------------

def test_daily_loss_limit_exactly_hit_halts(engine):
    events = engine.update_equity(98_000.0)  # exactly -2%
    assert [e.kind for e in events] == ["daily_halt"]
    assert engine.halted_today
    assert not entry(engine).approved


def test_daily_loss_just_under_limit_does_not_halt(engine):
    assert engine.update_equity(98_000.01) == []
    assert entry(engine).approved


def test_daily_halt_blocks_exits_too_but_clears_next_day(engine):
    engine.update_equity(97_000.0)
    pos = {"AAPL": Position("AAPL", 10, 100.0, DAY)}
    assert not engine.check_exit("AAPL", 10, acct(positions=pos), mid_session(DAY), make_session(DAY)).approved
    nxt = DAY + timedelta(days=1)
    engine.start_day(nxt, 97_000.0)
    assert not engine.halted_today
    assert engine.check_entry("AAPL", 100, 95, 1.0, acct(97_000, 97_000), mid_session(nxt),
                              make_session(nxt)).approved


def test_daily_halt_event_fires_once(engine):
    assert len(engine.update_equity(97_000.0)) == 1
    assert engine.update_equity(96_500.0) == []


# ---- drawdown kill switch ---------------------------------------------------------

def test_kill_switch_exactly_at_limit(engine):
    engine.update_equity(120_000.0)          # new peak
    nxt = DAY + timedelta(days=1)
    engine.start_day(nxt, 120_000.0)
    events = engine.update_equity(108_000.0)  # exactly -10% from peak
    assert "kill_switch" in [e.kind for e in events]
    assert engine.state.kill_switch


def test_kill_switch_survives_new_day_until_manual_reset(engine):
    engine.update_equity(89_000.0)
    assert engine.state.kill_switch
    later = DAY + timedelta(days=3)
    engine.start_day(later, 89_000.0)
    d = engine.check_entry("AAPL", 100, 95, 1.0, acct(89_000, 89_000), mid_session(later), make_session(later))
    assert not d.approved and "kill switch" in d.reasons[0]
    engine.reset_kill_switch(89_000.0)
    assert engine.check_entry("AAPL", 100, 95, 1.0, acct(89_000, 89_000), mid_session(later),
                              make_session(later)).approved


def test_kill_switch_state_round_trips(engine):
    engine.update_equity(85_000.0)
    restored = RiskState.from_dict(engine.state.to_dict())
    assert restored.kill_switch and restored.peak_equity == 100_000


# ---- order count ------------------------------------------------------------------

def test_max_orders_per_day_exactly(engine, cfg):
    for _ in range(cfg.risk.max_orders_per_day - 1):
        engine.record_order()
    assert entry(engine).approved            # 20th order allowed
    engine.record_order()
    d = entry(engine)                        # 21st rejected
    assert not d.approved and "max orders" in d.reasons[-1]


def test_order_count_resets_each_day(engine, cfg):
    for _ in range(cfg.risk.max_orders_per_day):
        engine.record_order()
    nxt = DAY + timedelta(days=1)
    engine.start_day(nxt, 100_000)
    assert engine.state.orders_today == 0


def test_start_day_is_idempotent(engine):
    engine.record_order()
    engine.start_day(DAY, 50_000)
    assert engine.state.orders_today == 1 and engine.state.day_start_equity == 100_000


# ---- market hours -----------------------------------------------------------------

def test_market_closed_no_session(engine):
    d = entry(engine, session=None)
    assert not d.approved and "market closed" in d.reasons[-1]


def test_market_closed_before_open_and_after_close(engine):
    s = make_session(DAY)
    assert not entry(engine, now=s.open - timedelta(minutes=1)).approved
    assert not entry(engine, now=s.close).approved


def test_first_and_last_15_minutes_blocked(engine):
    s = make_session(DAY)
    assert not entry(engine, now=s.open + timedelta(minutes=14, seconds=59)).approved
    assert entry(engine, now=s.open + timedelta(minutes=15)).approved
    assert entry(engine, now=s.close - timedelta(minutes=15)).approved
    assert not entry(engine, now=s.close - timedelta(minutes=14)).approved
