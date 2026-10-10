"""Paper-trading loop.

    python -m scripts.run_paper             # run all day, every trading day
    python -m scripts.run_paper --dry-run   # log decisions, place no orders
    python -m scripts.run_paper --once      # one cycle now (e.g. from cron), then exit
    python -m scripts.run_paper --reset-kill-switch   # after you've reviewed a kill-switch trip
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

from bot.broker import AlpacaPaperBroker
from bot.config import load_config, load_paper_keys
from bot.data import AlpacaData
from bot.risk import RiskEngine
from bot.logging_setup import alert, log_decision, setup_logging
from bot.state import load_state, save_state
from bot.strategies import build_strategy
from bot.trader import Trader


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="log decisions without placing or cancelling orders")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--strategy", default=None, help="override strategies.default")
    ap.add_argument("--reset-kill-switch", action="store_true",
                    help="clear a tripped kill switch after a human review, then exit")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)  # raises if live trading is requested
    setup_logging(cfg.path("log_dir"))
    key, secret = load_paper_keys()
    broker = AlpacaPaperBroker(cfg, key, secret)
    data = AlpacaData(key, secret, cfg.resolve(cfg.data["cache_dir"]), cfg.data["history_feed"],
                      cfg.data["latest_feed"])

    state_path = cfg.path("state_file")
    trade_log = cfg.path("trade_log")
    if args.dry_run:
        # A dry run never touches the real state or trade log.
        state_path = state_path.with_suffix(".dryrun.json")
        trade_log = trade_log.with_name("trade_log.dryrun.csv")
    state = load_state(state_path)

    if args.reset_kill_switch:
        equity = broker.get_account().equity
        was = state.risk.kill_reason
        RiskEngine(cfg.risk, cfg.symbols, state=state.risk).reset_kill_switch(equity)
        save_state(state, state_path)
        log_decision("kill_switch_reset", previous_reason=was, new_peak_equity=equity)
        print(f"Kill switch cleared (was: {was or 'not set'}). Peak equity reset to {equity:,.2f}.")
        return 0

    name = args.strategy or cfg.default_strategy
    trader = Trader(cfg, broker, data, build_strategy(name, cfg.strategy_params(name)), state,
                    state_path, trade_log, dry_run=args.dry_run)
    log_decision("startup", strategy=name, dry_run=args.dry_run, paper=True,
                 kill_switch=state.risk.kill_switch)
    if state.risk.kill_switch:
        alert(f"kill switch is active: {state.risk.kill_reason}. Review, then run with --reset-kill-switch.")

    poll = int(cfg.run_loop["poll_seconds"])
    while True:
        try:
            clock = broker.get_clock()
            if not clock.is_open:
                if args.once:
                    print("Market is closed; nothing to do.")
                    return 0
                wait = (clock.next_open - clock.now) + timedelta(minutes=cfg.risk.skip_open_minutes + 1)
                secs = max(60, min(wait.total_seconds(), 3600))
                log_decision("market_closed_sleep", seconds=int(secs), next_open=clock.next_open)
                time.sleep(secs)
                continue
            session = broker.get_session(trader.today(clock.now))
            ok, why = trader.risk.market_hours_ok(clock.now, session)
            if ok:
                trader.run_daily_cycle(clock.now, session)
                trader.monitor(clock.now)
            else:
                log_decision("outside_trading_window", reason=why)
                if args.once:
                    print(f"Not in the trading window: {why}")
                    return 0
        except KeyboardInterrupt:
            log_decision("shutdown", reason="keyboard interrupt")
            return 0
        except Exception as e:  # never die silently
            alert(f"error in run loop: {type(e).__name__}: {e}")
            if args.once:
                raise
        if args.once:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    sys.exit(main())
