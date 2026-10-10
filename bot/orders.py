"""Order IDs and safe submission.

Every order gets a deterministic client_order_id built from what it is for
(strategy, session, symbol, side, purpose). Re-running the same decision on the
same day produces the same ID, so the broker can't accept it twice. Before
every submission attempt, including the first, we ask the broker whether that
ID already exists; a retry never blindly resubmits.
"""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Callable

from bot.broker import Broker, BrokerOrder, TransientBrokerError
from bot.logging_setup import log_decision

MAX_ID_LEN = 48


def make_client_order_id(strategy: str, session: date, symbol: str, side: str, purpose: str) -> str:
    raw = f"{strategy}-{session:%Y%m%d}-{symbol}-{side}-{purpose}"
    cid = re.sub(r"[^A-Za-z0-9_.-]", "", raw)
    if len(cid) > MAX_ID_LEN:
        raise ValueError(f"client_order_id too long: {cid}")
    return cid


def submit_idempotent(broker: Broker, client_order_id: str, submit: Callable[[], BrokerOrder],
                      attempts: int = 4, base_delay: float = 1.0,
                      sleep: Callable[[float], None] = time.sleep) -> tuple[BrokerOrder, bool]:
    """Submit unless an order with this ID already exists.

    Returns (order, submitted_now). Transient errors are retried with backoff,
    and each retry first checks whether the earlier attempt actually reached
    the broker.
    """
    last_error: Exception | None = None
    for i in range(attempts):
        try:
            existing = broker.get_order_by_client_id(client_order_id)
        except TransientBrokerError as e:
            last_error = e
            sleep(base_delay * 2 ** i)
            continue
        if existing is not None:
            log_decision("order_already_exists", client_order_id=client_order_id, status=existing.status,
                         attempt=i + 1)
            return existing, False
        try:
            return submit(), True
        except TransientBrokerError as e:
            last_error = e
            log_decision("order_submit_retry", client_order_id=client_order_id, attempt=i + 1, error=str(e))
            sleep(base_delay * 2 ** i)
    raise TransientBrokerError(f"giving up on {client_order_id} after {attempts} attempts: {last_error}")
