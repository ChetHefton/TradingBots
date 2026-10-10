"""Run every strategy and buy-and-hold SPY over the build and test periods,
plus the rebalance-day sensitivity check, and write reports/.

    python -m scripts.backtest                # real Alpaca data (needs .env keys)
    python -m scripts.backtest --synthetic    # random data, to check the plumbing only
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone

from bot.backtest.engine import buy_and_hold, run_backtest
from bot.backtest.metrics import compute
from bot.backtest.report import comparison_rows, equity_chart, save_table
from bot.config import load_config, load_paper_keys
from bot.data import AlpacaData, synthetic_bars
from bot.strategies import STRATEGIES, build_strategy
from bot.trade_log import write_trade_log


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--synthetic", action="store_true",
                    help="use random-walk data (no keys needed; results are meaningless)")
    ap.add_argument("--refresh", action="store_true", help="re-download data instead of using the cache")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override a config value for this run, e.g. --set risk.max_position_pct=0.2")
    ap.add_argument("--config", default=None, help="path to settings.yaml")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config, overrides=args.set)
    bt = cfg.backtest
    d = lambda s: date.fromisoformat(str(s))  # noqa: E731
    data_start, build_start, build_end, test_start = (d(bt[k]) for k in
                                                      ("data_start", "build_start", "build_end", "test_start"))
    test_end = d(bt["test_end"]) if bt["test_end"] else datetime.now(timezone.utc).date() - timedelta(days=1)

    symbols = list(cfg.symbols)
    if args.synthetic:
        bars = synthetic_bars(symbols, data_start, test_end)
        note = "SYNTHETIC RANDOM DATA - checks the plumbing only; says nothing about real markets"
    else:
        key, secret = load_paper_keys()
        src = AlpacaData(key, secret, cfg.resolve(cfg.data["cache_dir"]),
                         cfg.data["history_feed"], cfg.data["latest_feed"])
        bars = src.get_daily_bars(symbols, data_start, test_end, refresh=args.refresh)
        note = f"Alpaca daily bars ({cfg.data['history_feed']}), split+dividend adjusted"
    missing = [s for s in symbols if bars.get(s) is None or bars[s].empty]
    if missing:
        print(f"WARNING: no data for {missing}; they are left out of the universe", file=sys.stderr)
        bars = {s: df for s, df in bars.items() if s not in missing}

    first = {s: df.index[0].date() for s, df in bars.items()}
    late = {s: f for s, f in first.items() if f > build_start}
    if late:
        print(f"NOTE: these symbols start after the build period begins: {late}", file=sys.stderr)

    out = cfg.resolve(bt["report_dir"])
    periods = {"build": (build_start, build_end), "test": (test_start, test_end)}
    spy = cfg.market_filter["symbol"]
    all_results = {}
    report = [f"# Backtest report\n\nData: {note}\n",
              f"Costs per side: {cfg.costs.per_side:.3%} (slippage {cfg.costs.slippage_pct:.2%} "
              f"+ half of {cfg.costs.spread_pct:.2%} spread). Starting capital "
              f"{float(bt['initial_capital']):,.0f}.\n"]
    if args.set:
        report.append(f"Config overrides for this run: {args.set}\n")

    for label, (start, end) in periods.items():
        results = []
        for name in STRATEGIES:
            r = run_backtest(build_strategy(name, cfg.strategy_params(name)), bars, cfg, start, end)
            results.append(r)
            write_trade_log(r.fills, out / f"trades_{label}_{name}.csv")
        results.append(buy_and_hold(bars[spy], cfg, start, end))
        all_results[label] = results
        table = save_table(comparison_rows(results), out / f"comparison_{label}")
        heading = "Build period" if label == "build" else "Test period (held out)"
        report.append(f"## {heading}: {start} to {end}\n\n{table}\n")
        for r in results:
            for day, kind, msg in r.events:
                report.append(f"- {r.name}: {kind} on {day}: {msg}")
        report.append("")

    # Sensitivity: shift the momentum rebalance day.
    sens_rows = []
    for label, (start, end) in periods.items():
        for off in bt["sensitivity_offsets"]:
            params = cfg.strategy_params("momentum") | {"rebalance_offset": int(off)}
            r = run_backtest(build_strategy("momentum", params), bars, cfg, start, end,
                             name=f"momentum offset {off}")
            m = compute(r)
            sens_rows.append({"Period": label, "Trading day of month": str(int(off) + 1),
                              **{k: v for k, v in m.as_row().items() if k != "Strategy"}})
    sens_md = save_table(sens_rows, out / "sensitivity_momentum")
    report.append("## Sensitivity: momentum rebalance day\n\nSame strategy, trading on the 1st to 6th "
                  "trading day of each month. Large differences between rows mean the result "
                  "depends on luck of timing.\n\n" + sens_md + "\n")

    equity_chart({f"Build {build_start} to {build_end}": all_results["build"],
                  f"Test {test_start} to {test_end}": all_results["test"]},
                 out / "equity_curves.png", title_note=note)
    (out / "report.md").write_text("\n".join(report) + "\n")
    print("\n".join(report))
    print(f"\nWrote {out}/report.md, comparison_*.csv, sensitivity_momentum.csv, equity_curves.png, trades_*.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
