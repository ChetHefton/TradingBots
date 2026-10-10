"""Paper-trading cycle: reconcile with the broker, protect every position with
a stop, enforce the risk limits, and act on the strategy's signals.

Order of operations each trading day (once, after the first 15 minutes):
  1. Read account; start the risk day from the broker's previous-close equity.
  2. Reconcile positions and open orders from the broker (the broker wins).
  3. Make sure every position has a resting stop order.
  4. If halted or killed, stop here.
  5. Build bars through yesterday's close, run the strategy and market filter.
  6. Sells first (cancel the symbol's stop, then sell), then buys with an
     attached stop. Every order goes through the risk engine and is logged.

During the rest of the session `monitor()` re-checks equity against the daily
loss limit and the kill switch, and re-checks that every position has a stop.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

import pandas as pd

from bot import indicators as ind
from bot import market_filter
from bot.backtest.engine import history_bars_needed
from bot.broker import Broker, BrokerError, BrokerOrder
from bot.config import Config
from bot.logging_setup import alert, log_decision
from bot.models import AccountView, Position, Side, Signal
from bot.orders import make_client_order_id, submit_idempotent
from bot.risk import RiskEngine, RiskEvent, Session
from bot.state import BotState, PositionMeta, save_state
from bot.strategies.base import Strategy, StrategyContext
from bot.trade_log import Fill, append_fill

NY = ZoneInfo("America/New_York")


class DataSource(Protocol):
    def get_daily_bars(self, symbols: list[str], start: date, end: date | None = None) -> dict[str, pd.DataFrame]: ...
    def get_latest_price(self, symbol: str) -> float | None: ...


class Trader:
    def __init__(self, cfg: Config, broker: Broker, data: DataSource, strategy: Strategy,
                 state: BotState, state_path: Path, trade_log_path: Path, dry_run: bool = False,
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.broker = broker
        self.data = data
        self.strategy = strategy
        self.state = state
        self.state_path = state_path
        self.trade_log_path = trade_log_path
        self.dry_run = dry_run
        self.sleep = sleep
        self.risk = RiskEngine(cfg.risk, cfg.symbols, cost_per_side=cfg.costs.per_side, state=state.risk)

    # ---- helpers -----------------------------------------------------------------

    def save(self) -> None:
        save_state(self.state, self.state_path)

    @staticmethod
    def today(now: datetime) -> date:
        return now.astimezone(NY).date()

    def handle_risk_events(self, events: list[RiskEvent]) -> None:
        for ev in events:
            alert(f"{ev.kind}: {ev.message}", kind=ev.kind)
            self.cancel_non_protective_orders(reason=ev.kind)
        if events:
            self.save()

    def cancel_non_protective_orders(self, reason: str) -> None:
        """Cancel pending entries and exits but leave resting stop-loss orders,
        so positions stay protected while the bot is halted."""
        for o in self.broker.get_open_orders():
            if o.side == "sell" and o.type == "stop":
                continue
            log_decision("cancel_order", order_id=o.id, client_order_id=o.client_order_id,
                         symbol=o.symbol, reason=reason, dry_run=self.dry_run)
            if not self.dry_run:
                self.broker.cancel_order(o.id)

    # ---- reconcile ---------------------------------------------------------------

    def reconcile(self, today: date) -> dict[str, Position]:
        """Rebuild positions from the broker. Unexpected positions are adopted
        and alerted; positions that vanished (stopped out or closed by hand)
        are logged to the trade log and forgotten."""
        broker_pos = self.broker.get_positions()
        positions: dict[str, Position] = {}

        for sym, bp in broker_pos.items():
            if bp.qty <= 0:
                alert(f"short or zero position in {sym} ({bp.qty}); the bot never shorts. Close it by hand.",
                      symbol=sym)
                continue
            if sym not in self.cfg.symbols:
                alert(f"position in {sym} is not on the allowlist; the bot will not trade it.", symbol=sym)
                continue
            meta = self.state.positions.get(sym)
            if meta is None:
                alert(f"broker reports a position the bot did not expect: {bp.qty} {sym}; adopting it",
                      symbol=sym, qty=bp.qty, avg_entry_price=bp.avg_entry_price)
                meta = PositionMeta(entry_date=today.isoformat(), strategy="unknown", stop_price=None,
                                    lot_id=f"adopted-{sym}-{today:%Y%m%d}", entry_price=bp.avg_entry_price)
                self.state.positions[sym] = meta
            if meta.entry_price is None:
                meta.entry_price = bp.avg_entry_price
            positions[sym] = Position(sym, bp.qty, bp.avg_entry_price, date.fromisoformat(meta.entry_date),
                                      meta.stop_price)

        for sym in list(self.state.positions):
            if sym in broker_pos:
                continue
            meta = self.state.positions.pop(sym)
            sells = self.broker.get_filled_sells(sym, date.fromisoformat(meta.entry_date))
            log_decision("position_closed_outside_cycle", symbol=sym, sells=[vars(s) for s in sells])
            for s in sells:
                if not s.filled_avg_price:
                    alert(f"sell of {sym} ({s.client_order_id}) has no fill price; add it to the trade log by hand",
                          symbol=sym)
                    continue
                self._log_fill(today, sym, "sell", s.filled_qty, s.filled_avg_price,
                               reason="stop or manual close", lot_id=meta.lot_id, fill_id=s.client_order_id,
                               entry_price=meta.entry_price)
        self.save()
        log_decision("reconciled", positions={s: p.qty for s, p in positions.items()})
        return positions

    # ---- stops ---------------------------------------------------------------------

    def _default_stop(self, sym: str, today: date) -> float | None:
        p = self.cfg.strategy_params(self.strategy.name)
        days, mult = p.get("atr_days", 14), p.get("atr_stop_mult", 2.0)
        bars = self.data.get_daily_bars([sym], today - timedelta(days=60), today - timedelta(days=1)).get(sym)
        if bars is None or len(bars) < days + 1:
            return None
        a = ind.last(ind.atr(bars, days))
        return float(bars["close"].iloc[-1]) - mult * a if a else None

    def protect_positions(self, positions: dict[str, Position], today: date) -> None:
        """Make sure each position has a resting stop for its full quantity."""
        open_orders = self.broker.get_open_orders()
        for sym, pos in positions.items():
            # Any pending sell holds the shares (a second order for them would be
            # rejected), and a pending market sell is about to close the position.
            covered = sum(o.qty for o in open_orders if o.symbol == sym and o.side == "sell")
            if covered >= pos.qty:
                continue
            stop = pos.stop_price or self._default_stop(sym, today)
            if stop is None or stop <= 0:
                alert(f"{sym} has no stop and none could be computed; protect it by hand", symbol=sym)
                continue
            qty = pos.qty - covered
            cid = self._fresh_protect_id(today, sym)
            log_decision("place_protective_stop", symbol=sym, qty=qty, stop_price=round(stop, 2),
                         client_order_id=cid, dry_run=self.dry_run)
            if self.dry_run or cid is None:
                if cid is None:
                    alert(f"too many protective stops for {sym} today; protect it by hand", symbol=sym)
                continue
            try:
                _, submitted = submit_idempotent(
                    self.broker, cid, lambda: self.broker.submit_stop_sell(sym, qty, stop, cid), sleep=self.sleep)
            except BrokerError as e:
                # Keep going: one rejected stop must not leave the other positions unprotected.
                alert(f"could not place stop for {sym}: {e}", symbol=sym)
                continue
            # Protective stops are counted but never blocked by the order limit:
            # an unprotected position is worse than one extra order.
            if submitted:
                self.risk.record_order()
            meta = self.state.positions.get(sym)
            if meta:
                meta.stop_price = stop
        self.save()

    def _fresh_protect_id(self, today: date, sym: str, max_tries: int = 20) -> str | None:
        """A client_order_id not used yet today. Reusing the ID of a stop that
        was cancelled would make submit_idempotent return the dead order."""
        for n in range(1, max_tries + 1):
            cid = make_client_order_id("protect", today, sym, "sell", f"p{n}")
            if self.dry_run or self.broker.get_order_by_client_id(cid) is None:
                return cid
        return None

    # ---- daily cycle ---------------------------------------------------------------

    def _bars(self, as_of: date, n: int) -> dict[str, pd.DataFrame]:
        start = as_of - timedelta(days=int(n * 1.6) + 30)
        bars = self.data.get_daily_bars(list(self.cfg.symbols), start, as_of)
        return {s: df.loc[:pd.Timestamp(as_of)].tail(n) for s, df in bars.items() if df is not None and len(df)}

    def _wait_for_fill(self, cid: str) -> BrokerOrder | None:
        deadline = time.monotonic() + float(self.cfg.run_loop["fill_wait_seconds"])
        while True:
            o = self.broker.get_order_by_client_id(cid)
            if o and o.status == "filled":
                return o
            if o and o.status in ("canceled", "rejected", "expired"):
                return o
            if time.monotonic() >= deadline:
                return o
            self.sleep(2)

    def _wait_for_cancels(self, client_order_ids: list[str]) -> bool:
        deadline = time.monotonic() + float(self.cfg.run_loop["fill_wait_seconds"])
        while True:
            statuses = [getattr(self.broker.get_order_by_client_id(c), "status", "canceled")
                        for c in client_order_ids]
            if all(s in ("canceled", "expired", "rejected") for s in statuses):
                return True
            if any(s == "filled" for s in statuses) or time.monotonic() >= deadline:
                return False
            self.sleep(1)

    def _log_fill(self, day: date, sym: str, side: str, qty: int, price: float, reason: str, lot_id: str,
                  fill_id: str, entry_price: float | None = None) -> None:
        pnl = None
        if side == "sell" and entry_price is not None:
            pnl = round((price - entry_price) * qty, 2)
        append_fill(Fill(fill_id, day, sym, side, qty, price, self.strategy.name, reason, lot_id=lot_id,
                         realized_pnl=pnl), self.trade_log_path)

    def run_daily_cycle(self, now: datetime, session: Session | None) -> None:
        today = self.today(now)
        if self.state.last_cycle_day == today.isoformat():
            return
        ok, why = self.risk.market_hours_ok(now, session)
        if not ok:
            log_decision("cycle_skipped", reason=why)
            return

        acct = self.broker.get_account()
        self.risk.start_day(today, acct.last_equity)
        if acct.multiplier > 1 or acct.shorting_enabled:
            log_decision("account_allows_margin_or_shorting", logging.WARNING, multiplier=acct.multiplier,
                         shorting_enabled=acct.shorting_enabled,
                         note="the bot sizes from cash and never shorts, but turning these off in the "
                              "Alpaca dashboard is safer")
        self.handle_risk_events(self.risk.update_equity(acct.equity))

        positions = self.reconcile(today)
        self.protect_positions(positions, today)

        if self.risk.state.kill_switch or self.risk.halted_today:
            log_decision("cycle_halted", kill_switch=self.risk.state.kill_switch,
                         daily_halt=self.risk.halted_today, reason=self.risk.state.kill_reason)
            self.state.last_cycle_day = today.isoformat()
            self.save()
            return

        sessions = self.broker.get_sessions(today - timedelta(days=60), today)
        if not sessions or sessions[-1] != today or len(sessions) < 2:
            log_decision("cycle_skipped", reason="today is not a trading session")
            return
        as_of = sessions[-2]
        n = history_bars_needed(self.strategy, self.cfg.market_filter["sma_days"])
        bars = self._bars(as_of, n)
        stale = [s for s, df in bars.items() if df.index[-1].date() != as_of]
        if stale:
            log_decision("stale_bars", logging.WARNING, symbols=stale, expected=as_of)
        universe = tuple(s for s in self.cfg.symbols if s in bars)
        ctx = StrategyContext(as_of=as_of, next_session=today, sessions=pd.DatetimeIndex(sessions),
                              bars=bars, positions=positions, universe=universe)
        signals = self.strategy.generate(ctx)
        mf = self.cfg.market_filter
        fr = market_filter.evaluate(bars, mf["symbol"], mf["sma_days"])
        kept, blocked = market_filter.apply(signals, fr)
        log_decision("signals", strategy=self.strategy.name, as_of=as_of, market_risk_on=fr.risk_on,
                     spy_close=fr.close, spy_sma=fr.sma,
                     signals=[vars(s) for s in kept], blocked_by_market_filter=[s.symbol for s in blocked])

        for sig in [s for s in kept if s.side is Side.SELL]:
            self._execute_sell(sig, positions, today, now, session)
        acct = self.broker.get_account()
        if not self.dry_run:
            held = self.broker.get_positions()
            positions = {s: p for s, p in positions.items() if s in held}
        for sig in [s for s in kept if s.side is Side.BUY]:
            acct = self._execute_buy(sig, positions, acct, today, now, session, bars)

        self.state.last_cycle_day = today.isoformat()
        self.save()
        log_decision("cycle_done", day=today, orders_today=self.risk.state.orders_today)

    def _execute_sell(self, sig: Signal, positions: dict[str, Position], today: date, now: datetime,
                      session: Session | None) -> None:
        acct = self.broker.get_account()
        view = AccountView(acct.equity, acct.cash, positions)
        pos = positions.get(sig.symbol)
        d = self.risk.check_exit(sig.symbol, pos.qty if pos else 0, view, now, session)
        cid = make_client_order_id(self.strategy.name, today, sig.symbol, "sell", "exit")
        log_decision("sell_decision", symbol=sig.symbol, reason=sig.reason, inputs=sig.inputs,
                     approved=d.approved, qty=d.qty, risk_reasons=d.reasons, checks=d.checks,
                     client_order_id=cid, dry_run=self.dry_run)
        if not d.approved or self.dry_run:
            return
        existing = self.broker.get_order_by_client_id(cid)
        if existing is None:
            # The stop holds the shares; it must be fully cancelled before the
            # sell, or the broker rejects the sell for insufficient quantity.
            stops = [o for o in self.broker.get_open_orders() if o.symbol == sig.symbol and o.side == "sell"]
            try:
                for o in stops:
                    self.broker.cancel_order(o.id)
            except BrokerError as e:
                alert(f"could not cancel stop for {sig.symbol}; not selling: {e}", symbol=sig.symbol)
                return
            if not self._wait_for_cancels([o.client_order_id for o in stops]):
                alert(f"stop for {sig.symbol} did not cancel in time; sell skipped, monitor will re-protect",
                      symbol=sig.symbol)
                return
        try:
            _, submitted = submit_idempotent(
                self.broker, cid, lambda: self.broker.submit_market_sell(sig.symbol, d.qty, cid), sleep=self.sleep)
        except BrokerError as e:
            alert(f"sell of {sig.symbol} rejected: {e}; monitor will re-place its stop", symbol=sig.symbol)
            return
        if submitted:
            self.risk.record_order()
        self.save()
        filled = self._wait_for_fill(cid)
        meta = self.state.positions.get(sig.symbol)
        if filled and filled.status == "filled" and filled.filled_avg_price:
            self._log_fill(today, sig.symbol, "sell", filled.filled_qty, filled.filled_avg_price,
                           sig.reason, meta.lot_id if meta else "", cid, meta.entry_price if meta else None)
            self.state.positions.pop(sig.symbol, None)
        else:
            alert(f"sell of {sig.symbol} not filled yet ({filled.status if filled else 'unknown'}); "
                  "its stop was cancelled, check it", symbol=sig.symbol)
        self.save()

    def _execute_buy(self, sig: Signal, positions: dict[str, Position], acct, today: date, now: datetime,
                     session: Session | None, bars: dict[str, pd.DataFrame]):
        price = self.data.get_latest_price(sig.symbol)
        if price is None or not math.isfinite(price):
            price = float(bars[sig.symbol]["close"].iloc[-1])
        stop = round(price - sig.stop_distance, 2)
        # Alpaca lowers cash only when an order fills, so reserve cash for
        # buys that are still open; otherwise a margin account would overspend.
        reserved = 0.0
        for o in self.broker.get_open_orders():
            if o.side == "buy":
                ref = self.data.get_latest_price(o.symbol) or (
                    float(bars[o.symbol]["close"].iloc[-1]) if o.symbol in bars else price)
                reserved += (o.qty - o.filled_qty) * ref
        view = AccountView(acct.equity, acct.cash - reserved, positions)
        d = self.risk.check_entry(sig.symbol, price, stop, sig.target_weight, view, now, session)
        cid = make_client_order_id(self.strategy.name, today, sig.symbol, "buy", "entry")
        log_decision("buy_decision", symbol=sig.symbol, reason=sig.reason, inputs=sig.inputs, price=price,
                     stop_price=stop, approved=d.approved, qty=d.qty, risk_reasons=d.reasons,
                     checks=d.checks, client_order_id=cid, dry_run=self.dry_run)
        if not d.approved or self.dry_run:
            return acct
        self.state.positions[sig.symbol] = PositionMeta(entry_date=today.isoformat(), strategy=self.strategy.name,
                                                        stop_price=stop, lot_id=cid)
        self.save()
        try:
            _, submitted = submit_idempotent(
                self.broker, cid, lambda: self.broker.submit_market_buy_with_stop(sig.symbol, d.qty, stop, cid),
                sleep=self.sleep)
        except BrokerError as e:
            alert(f"buy of {sig.symbol} rejected: {e}", symbol=sig.symbol)
            self.state.positions.pop(sig.symbol, None)
            self.save()
            return acct
        if submitted:
            self.risk.record_order()
        filled = self._wait_for_fill(cid)
        if filled and filled.status == "filled":
            self.state.positions[sig.symbol].entry_price = filled.filled_avg_price
            self._log_fill(today, sig.symbol, "buy", filled.filled_qty, filled.filled_avg_price or price,
                           sig.reason, cid, cid)
            positions[sig.symbol] = Position(sig.symbol, filled.filled_qty, filled.filled_avg_price or price,
                                             today, stop)
        else:
            log_decision("buy_not_filled_yet", logging.WARNING, symbol=sig.symbol,
                         status=filled.status if filled else None)
        self.save()
        return self.broker.get_account()

    # ---- intraday monitoring -------------------------------------------------------

    def monitor(self, now: datetime) -> None:
        today = self.today(now)
        acct = self.broker.get_account()
        self.risk.start_day(today, acct.last_equity)
        self.handle_risk_events(self.risk.update_equity(acct.equity))
        positions = self.reconcile(today)
        self.protect_positions(positions, today)
