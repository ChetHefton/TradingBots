"""Risk engine. Every order passes through here; no strategy can bypass it.

The engine is pure logic with no broker access, so the backtester and the
paper loop use exactly the same code, and every rule is unit-tested.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from bot.config import RiskLimits
from bot.models import AccountView


@dataclass
class RiskState:
    """Everything the engine must remember across restarts."""

    peak_equity: float = 0.0
    day: str | None = None            # ISO date of the current trading day
    day_start_equity: float = 0.0     # equity at the previous close
    orders_today: int = 0
    daily_halt_day: str | None = None  # halted for this ISO date
    kill_switch: bool = False
    kill_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RiskState":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass(frozen=True)
class Session:
    """Regular-hours session for one trading day (timezone-aware)."""

    open: datetime
    close: datetime


@dataclass
class RiskDecision:
    approved: bool
    qty: int = 0
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def reject(self, reason: str) -> "RiskDecision":
        self.approved = False
        self.qty = 0
        self.reasons.append(reason)
        return self


@dataclass(frozen=True)
class RiskEvent:
    kind: str  # "daily_halt" or "kill_switch"
    message: str


class RiskEngine:
    def __init__(self, limits: RiskLimits, allowlist: tuple[str, ...] | list[str],
                 cost_per_side: float = 0.0, state: RiskState | None = None):
        self.limits = limits
        self.allowlist = frozenset(s.upper() for s in allowlist)
        self.cost_per_side = cost_per_side
        self.state = state or RiskState()

    # ---- day and equity tracking -------------------------------------------------

    def start_day(self, day: date, prev_close_equity: float) -> None:
        """Call once at the start of each trading day. Resets the daily counters.
        Idempotent: calling again on the same day changes nothing."""
        iso = day.isoformat()
        if self.state.day == iso:
            return
        self.state.day = iso
        self.state.day_start_equity = prev_close_equity
        self.state.orders_today = 0
        if self.state.peak_equity <= 0:
            self.state.peak_equity = prev_close_equity

    def update_equity(self, equity: float) -> list[RiskEvent]:
        """Feed the latest equity. Updates the peak and trips the daily halt or
        the kill switch when a limit is reached (reaching it exactly counts)."""
        events: list[RiskEvent] = []
        s = self.state
        s.peak_equity = max(s.peak_equity, equity)

        if not s.kill_switch and s.peak_equity > 0:
            drawdown = 1 - equity / s.peak_equity
            if drawdown >= self.limits.max_drawdown_pct - 1e-12:
                s.kill_switch = True
                s.kill_reason = (
                    f"drawdown {drawdown:.2%} from peak {s.peak_equity:,.2f} "
                    f">= limit {self.limits.max_drawdown_pct:.2%}"
                )
                events.append(RiskEvent("kill_switch", s.kill_reason))

        if s.day and s.daily_halt_day != s.day and s.day_start_equity > 0:
            day_loss = 1 - equity / s.day_start_equity
            if day_loss >= self.limits.daily_loss_limit_pct - 1e-12:
                s.daily_halt_day = s.day
                events.append(RiskEvent(
                    "daily_halt",
                    f"daily loss {day_loss:.2%} >= limit {self.limits.daily_loss_limit_pct:.2%}; "
                    f"halted for {s.day}",
                ))
        return events

    @property
    def halted_today(self) -> bool:
        return self.state.day is not None and self.state.daily_halt_day == self.state.day

    def reset_kill_switch(self, current_equity: float) -> None:
        """Manual reset after a human review. The peak restarts from current
        equity so the switch doesn't trip again immediately."""
        self.state.kill_switch = False
        self.state.kill_reason = ""
        self.state.peak_equity = current_equity

    def record_order(self) -> None:
        self.state.orders_today += 1

    # ---- checks ------------------------------------------------------------------

    def _common_checks(self, d: RiskDecision, symbol: str, now: datetime | None,
                       session: Session | None) -> RiskDecision:
        s = self.state
        d.checks["kill_switch"] = s.kill_switch
        if s.kill_switch:
            return d.reject(f"kill switch active: {s.kill_reason}")
        d.checks["daily_halt"] = self.halted_today
        if self.halted_today:
            return d.reject("daily loss limit hit; halted until next trading day")
        ok, why = self.market_hours_ok(now, session)
        d.checks["market_hours"] = why
        if not ok:
            return d.reject(why)
        d.checks["orders_today"] = s.orders_today
        if s.orders_today >= self.limits.max_orders_per_day:
            return d.reject(f"max orders per day reached ({self.limits.max_orders_per_day})")
        d.checks["allowlist"] = symbol.upper() in self.allowlist
        if symbol.upper() not in self.allowlist:
            return d.reject(f"{symbol} is not in the allowlist")
        return d

    def market_hours_ok(self, now: datetime | None, session: Session | None) -> tuple[bool, str]:
        if session is None or now is None:
            return False, "market closed (no session today)"
        start = session.open + timedelta(minutes=self.limits.skip_open_minutes)
        end = session.close - timedelta(minutes=self.limits.skip_close_minutes)
        if now < session.open or now >= session.close:
            return False, "market closed"
        if now < start:
            return False, f"within first {self.limits.skip_open_minutes} minutes of session"
        if now > end:
            return False, f"within last {self.limits.skip_close_minutes} minutes of session"
        return True, "ok"

    def check_entry(self, symbol: str, price: float, stop_price: float, target_weight: float,
                    account: AccountView, now: datetime | None, session: Session | None) -> RiskDecision:
        """Size and approve (or reject) a long entry with a stop."""
        d = RiskDecision(approved=True)
        d.checks.update(symbol=symbol, price=price, stop_price=stop_price, target_weight=target_weight,
                        equity=account.equity, cash=account.cash)
        self._common_checks(d, symbol, now, session)
        if not d.approved:
            return d
        if symbol in account.positions:
            return d.reject(f"already holding {symbol}; adding to positions is not allowed")
        if not (price > 0 and math.isfinite(price)):
            return d.reject(f"invalid price {price}")
        if not (0 < stop_price < price):
            return d.reject(f"stop {stop_price} must be below entry price {price} and above 0")
        if account.equity <= 0:
            return d.reject("equity is not positive")

        lim = self.limits
        per_share_risk = price - stop_price
        risk_qty = math.floor(account.equity * lim.max_risk_per_trade_pct / per_share_risk)
        cap_qty = math.floor(account.equity * lim.max_position_pct / price)
        target_qty = math.floor(account.equity * max(target_weight, 0.0) / price)
        # Cash account: never spend more than settled cash, including costs.
        cash_qty = math.floor(max(account.cash, 0.0) / (price * (1 + self.cost_per_side)))
        qty = min(risk_qty, cap_qty, target_qty, cash_qty)
        d.checks.update(risk_qty=risk_qty, cap_qty=cap_qty, target_qty=target_qty, cash_qty=cash_qty)
        if qty < 1:
            return d.reject("position size rounds to zero after limits")
        d.qty = qty
        d.reasons.append(f"sized to {qty} (min of risk {risk_qty}, cap {cap_qty}, "
                         f"target {target_qty}, cash {cash_qty})")
        return d

    def check_exit(self, symbol: str, qty: int, account: AccountView, now: datetime | None,
                   session: Session | None) -> RiskDecision:
        """Approve a sell. Never sells more than is held (no shorting)."""
        d = RiskDecision(approved=True)
        d.checks.update(symbol=symbol, requested_qty=qty)
        self._common_checks(d, symbol, now, session)
        if not d.approved:
            return d
        pos = account.positions.get(symbol)
        if pos is None or pos.qty <= 0:
            return d.reject(f"no long position in {symbol} to sell")
        d.qty = min(qty, pos.qty)
        if d.qty < 1:
            return d.reject("sell quantity is zero")
        return d
