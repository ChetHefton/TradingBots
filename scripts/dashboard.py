"""Read-only terminal dashboard on http://127.0.0.1:8050

    python -m scripts.dashboard              # with paper account snapshot (needs .env keys)
    python -m scripts.dashboard --no-broker  # files only: backtest reports, logs, state
"""
from __future__ import annotations

import argparse
import sys

from bot.config import ConfigError, load_config, load_paper_keys
from bot.dashboard.server import DashboardData, broker_account_fn, make_server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--no-broker", action="store_true", help="don't contact Alpaca; show files only")
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    account_fn = None
    if not args.no_broker:
        try:
            from bot.broker import AlpacaPaperBroker

            key, secret = load_paper_keys()
            account_fn = broker_account_fn(AlpacaPaperBroker(cfg, key, secret))
        except ConfigError as e:
            print(f"No broker link: {e}", file=sys.stderr)

    server = make_server(DashboardData(cfg, account_fn), args.port)
    print(f"Dashboard (read-only) at http://127.0.0.1:{args.port}  — Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
