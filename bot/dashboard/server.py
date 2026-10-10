"""Read-only local dashboard.

Serves one page plus a few JSON endpoints built from files the bot already
writes (reports/, logs/, state/) and, if paper keys are configured, a cached
snapshot of the paper account. It binds to 127.0.0.1 and answers GET only:
there is no endpoint that can place, cancel or change anything.
"""
from __future__ import annotations

import csv
import json
import threading
import time
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from bot.config import Config
from bot.state import load_state

STATIC_DIR = Path(__file__).parent / "static"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
ACCOUNT_CACHE_SECONDS = 30


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with open(path) as f:
        lines = deque(f, maxlen=limit)
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()  # newest first
    return out


class DashboardData:
    def __init__(self, cfg: Config, account_fn: Callable[[], dict[str, Any]] | None = None):
        self.cfg = cfg
        self.account_fn = account_fn
        self._account_cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    # Account snapshot from the paper broker, cached so page refreshes don't hammer the API.
    def account(self) -> dict[str, Any]:
        if self.account_fn is None:
            return {"available": False, "reason": "no paper keys configured (or --no-broker)"}
        with self._lock:
            now = time.monotonic()
            if self._account_cache and now - self._account_cache[0] < ACCOUNT_CACHE_SECONDS:
                return self._account_cache[1]
            try:
                snap = {"available": True, **self.account_fn()}
            except Exception as e:  # dashboard must keep working when offline
                snap = {"available": False, "reason": f"{type(e).__name__}: {e}"}
            self._account_cache = (now, snap)
            return snap

    def summary(self) -> dict[str, Any]:
        state = load_state(self.cfg.path("state_file"))
        r = self.cfg.risk
        return {
            "mode": "paper",
            "strategy": self.cfg.default_strategy,
            "symbols": list(self.cfg.symbols),
            "limits": {
                "max_risk_per_trade_pct": r.max_risk_per_trade_pct,
                "max_position_pct": r.max_position_pct,
                "daily_loss_limit_pct": r.daily_loss_limit_pct,
                "max_drawdown_pct": r.max_drawdown_pct,
                "max_orders_per_day": r.max_orders_per_day,
                "skip_open_minutes": r.skip_open_minutes,
                "skip_close_minutes": r.skip_close_minutes,
            },
            "market_filter": self.cfg.market_filter,
            "state": state.to_dict(),
            "account": self.account(),
        }

    def backtest(self) -> dict[str, Any]:
        out_dir = self.cfg.resolve(self.cfg.backtest["report_dir"])
        meta_path = out_dir / "meta.json"
        curves: dict[str, dict[str, list[list[Any]]]] = {}
        for row in _read_csv(out_dir / "equity_curves.csv"):
            curves.setdefault(row["period"], {}).setdefault(row["strategy"], []).append(
                [row["date"], float(row["index"])])
        return {
            "available": meta_path.exists(),
            "meta": json.loads(meta_path.read_text()) if meta_path.exists() else None,
            "comparison": {p: _read_csv(out_dir / f"comparison_{p}.csv") for p in ("build", "test")},
            "sensitivity": _read_csv(out_dir / "sensitivity_momentum.csv"),
            "curves": curves,
        }

    def decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        return _tail_jsonl(self.cfg.path("log_dir") / "decisions.jsonl", max(1, min(limit, 2000)))

    def trades(self) -> list[dict[str, str]]:
        return _read_csv(self.cfg.path("trade_log"))


def make_handler(data: DashboardData) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingBotsDashboard/1"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any) -> None:
            self._send(HTTPStatus.OK, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in STATIC_FILES:
                    name, ctype = STATIC_FILES[url.path]
                    self._send(HTTPStatus.OK, (STATIC_DIR / name).read_bytes(), ctype)
                elif url.path == "/api/summary":
                    self._json(data.summary())
                elif url.path == "/api/backtest":
                    self._json(data.backtest())
                elif url.path == "/api/decisions":
                    self._json(data.decisions(int(q.get("limit", ["200"])[0])))
                elif url.path == "/api/trades":
                    self._json(data.trades())
                else:
                    self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            except Exception as e:
                self._send(HTTPStatus.INTERNAL_SERVER_ERROR, f"error: {e}".encode(), "text/plain")

        def _read_only(self) -> None:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read-only dashboard", "text/plain")

        do_POST = do_PUT = do_PATCH = do_DELETE = _read_only  # noqa: N815

    return Handler


def make_server(data: DashboardData, port: int) -> ThreadingHTTPServer:
    # Localhost only: the dashboard shows account data and has no authentication.
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(data))


def broker_account_fn(broker) -> Callable[[], dict[str, Any]]:
    """Read-only snapshot from the paper broker (account + positions)."""
    def fn() -> dict[str, Any]:
        a = broker.get_account()
        positions = broker.get_positions()
        return {
            "equity": a.equity, "cash": a.cash, "last_equity": a.last_equity,
            "positions": [vars(p) for p in positions.values()],
        }
    return fn
