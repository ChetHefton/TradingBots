"""Small JSON state file for the paper loop: risk state (peak equity, halts,
kill switch, order count) and what the bot knows about each position that the
broker doesn't (entry date, strategy, intended stop, opening order ID).

The broker is always the source of truth for what is held; this file only
adds metadata. It is written atomically so a crash can't leave half a file.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bot.risk import RiskState


@dataclass
class PositionMeta:
    entry_date: str
    strategy: str
    stop_price: float | None
    lot_id: str           # client_order_id of the opening buy
    entry_price: float | None = None


@dataclass
class BotState:
    risk: RiskState = field(default_factory=RiskState)
    positions: dict[str, PositionMeta] = field(default_factory=dict)
    last_cycle_day: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk.to_dict(),
            "positions": {s: vars(m) for s, m in self.positions.items()},
            "last_cycle_day": self.last_cycle_day,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BotState":
        return cls(
            risk=RiskState.from_dict(d.get("risk", {})),
            positions={s: PositionMeta(**m) for s, m in d.get("positions", {}).items()},
            last_cycle_day=d.get("last_cycle_day"),
        )


def load_state(path: Path) -> BotState:
    if not path.exists():
        return BotState()
    with open(path) as f:
        return BotState.from_dict(json.load(f))


def save_state(state: BotState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.replace(tmp, path)
