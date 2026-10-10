"""Order safety: unique IDs, no double submission, check-before-retry."""
from datetime import date

import pytest

from bot.broker import TransientBrokerError
from bot.orders import make_client_order_id, submit_idempotent
from tests.conftest import FakeBroker

NO_SLEEP = lambda s: None  # noqa: E731


def test_client_order_id_is_deterministic_and_unique_per_purpose():
    a = make_client_order_id("momentum", date(2026, 10, 1), "AAPL", "buy", "entry")
    assert a == make_client_order_id("momentum", date(2026, 10, 1), "AAPL", "buy", "entry")
    assert a != make_client_order_id("momentum", date(2026, 10, 2), "AAPL", "buy", "entry")
    assert a != make_client_order_id("momentum", date(2026, 10, 1), "AAPL", "sell", "exit")
    assert len(a) <= 48


def test_duplicate_order_attempt_is_not_resubmitted():
    b = FakeBroker()
    cid = "momentum-20261001-AAPL-buy-entry"
    submit = lambda: b.submit_market_buy_with_stop("AAPL", 10, 95.0, cid)  # noqa: E731
    _, first = submit_idempotent(b, cid, submit, sleep=NO_SLEEP)
    _, second = submit_idempotent(b, cid, submit, sleep=NO_SLEEP)
    assert first is True and second is False
    assert b.submissions.count(cid) == 1


def test_retry_after_timeout_checks_broker_first():
    """The first attempt reached the broker but the response timed out. The
    retry must find the existing order and not submit again."""
    b = FakeBroker()
    b.fail_next_submits = 1
    cid = "rsi2-20261001-MSFT-buy-entry"
    order, submitted_now = submit_idempotent(
        b, cid, lambda: b.submit_market_buy_with_stop("MSFT", 5, 90.0, cid), sleep=NO_SLEEP)
    assert submitted_now is False and order.client_order_id == cid
    assert b.submissions.count(cid) == 1


def test_retry_resubmits_when_first_attempt_was_lost():
    b = FakeBroker()
    b.fail_next_submits_lost = 1
    cid = "rsi2-20261001-MSFT-sell-exit"
    b.positions["MSFT"] = __import__("bot.broker", fromlist=["BrokerPosition"]).BrokerPosition("MSFT", 5, 100.0)
    order, submitted_now = submit_idempotent(
        b, cid, lambda: b.submit_market_sell("MSFT", 5, cid), sleep=NO_SLEEP)
    assert submitted_now is True and b.submissions.count(cid) == 2  # lost one, then the real one
    assert len([o for o in b.orders.values() if o.client_order_id == cid]) == 1


def test_gives_up_after_max_attempts():
    b = FakeBroker()
    b.fail_next_submits_lost = 10
    cid = "x-20261001-AAPL-buy-entry"
    with pytest.raises(TransientBrokerError):
        submit_idempotent(b, cid, lambda: b.submit_market_sell("AAPL", 1, cid), attempts=3, sleep=NO_SLEEP)
