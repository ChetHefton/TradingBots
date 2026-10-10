"""Structured logging: every decision is one JSON line in logs/decisions.jsonl,
with a short human-readable copy on the console."""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

LOGGER_NAME = "bot"


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if hasattr(o, "value"):
        return o.value
    if hasattr(o, "item"):
        return o.item()
    return str(o)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        return json.dumps(payload, default=_default)


def setup_logging(log_dir: Path, console_level: int = logging.INFO) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fh = logging.FileHandler(log_dir / "decisions.jsonl")
    fh.setFormatter(JsonFormatter())
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ch.setLevel(console_level)
    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def log_decision(event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log one decision with its inputs and reasons, e.g.
    log_decision("order_rejected", symbol="AAPL", reasons=[...], checks={...})."""
    get_logger().log(level, event, extra={"fields": fields})


def alert(message: str, **fields: Any) -> None:
    """Critical event: log it and, if ALERT_WEBHOOK_URL is set (for example an
    ntfy.sh topic URL), POST the message so it reaches your phone."""
    log_decision("ALERT", logging.CRITICAL, message=message, **fields)
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return
    try:
        requests.post(url, data=message.encode("utf-8"), timeout=10)
    except requests.RequestException as e:  # alerts must never crash the bot
        log_decision("alert_delivery_failed", logging.ERROR, error=str(e))
