"""Load settings.yaml and .env into typed objects.

All limits come from config/settings.yaml. API keys come from the environment
(.env). Live trading is refused here, before any broker object exists.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class LiveTradingDisabledError(RuntimeError):
    """Raised whenever anything asks for live trading. Phase 1 is paper only."""


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RiskLimits:
    max_risk_per_trade_pct: float
    max_position_pct: float
    daily_loss_limit_pct: float
    max_drawdown_pct: float
    max_orders_per_day: int
    skip_open_minutes: int
    skip_close_minutes: int


@dataclass(frozen=True)
class Costs:
    slippage_pct: float
    spread_pct: float

    @property
    def per_side(self) -> float:
        """Fractional cost charged on each fill: slippage plus half the spread."""
        return self.slippage_pct + self.spread_pct / 2


@dataclass
class Config:
    raw: dict[str, Any]
    risk: RiskLimits
    symbols: tuple[str, ...]
    costs: Costs
    root: Path = field(default=PROJECT_ROOT)

    @property
    def live_trading(self) -> bool:
        return bool(self.raw["mode"]["live_trading"])

    @property
    def market_filter(self) -> dict[str, Any]:
        return self.raw["market_filter"]

    def strategy_params(self, name: str) -> dict[str, Any]:
        return dict(self.raw["strategies"][name])

    @property
    def default_strategy(self) -> str:
        return self.raw["strategies"]["default"]

    @property
    def backtest(self) -> dict[str, Any]:
        return self.raw["backtest"]

    @property
    def data(self) -> dict[str, Any]:
        return self.raw["data"]

    @property
    def run_loop(self) -> dict[str, Any]:
        return self.raw["run_loop"]

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else self.root / p

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else self.root / p


def _apply_overrides(raw: dict[str, Any], overrides: list[str]) -> None:
    """Apply "a.b.c=value" overrides (value parsed as YAML)."""
    for item in overrides:
        if "=" not in item:
            raise ConfigError(f"Override must look like key.path=value, got {item!r}")
        key, value = item.split("=", 1)
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            if part not in node:
                raise ConfigError(f"Unknown config key: {key}")
            node = node[part]
        if parts[-1] not in node:
            raise ConfigError(f"Unknown config key: {key}")
        node[parts[-1]] = yaml.safe_load(value)


def _validate(raw: dict[str, Any]) -> None:
    r = raw["risk"]
    for key in ("max_risk_per_trade_pct", "max_position_pct", "daily_loss_limit_pct", "max_drawdown_pct"):
        if not 0 < float(r[key]) <= 1:
            raise ConfigError(f"risk.{key} must be in (0, 1], got {r[key]}")
    if int(r["max_orders_per_day"]) < 1:
        raise ConfigError("risk.max_orders_per_day must be >= 1")
    if not raw["symbols"]:
        raise ConfigError("symbols allowlist is empty")
    if raw["market_filter"]["symbol"] not in raw["symbols"]:
        raise ConfigError("market_filter.symbol must be in the symbols allowlist")


def load_config(path: Path | str | None = None, overrides: list[str] | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path) as f:
        raw = yaml.safe_load(f)
    raw = copy.deepcopy(raw)
    _apply_overrides(raw, overrides or [])
    _validate(raw)

    if raw["mode"]["live_trading"]:
        # TODO(live-mode): a future phase may add live trading here. It must use
        # separate keys, a separate config flag reviewed by a human, and its own
        # tests. Until then, any request for live trading is an error.
        raise LiveTradingDisabledError(
            "mode.live_trading is true, but live trading is not implemented in phase 1. "
            "Set it back to false."
        )

    r = raw["risk"]
    risk = RiskLimits(
        max_risk_per_trade_pct=float(r["max_risk_per_trade_pct"]),
        max_position_pct=float(r["max_position_pct"]),
        daily_loss_limit_pct=float(r["daily_loss_limit_pct"]),
        max_drawdown_pct=float(r["max_drawdown_pct"]),
        max_orders_per_day=int(r["max_orders_per_day"]),
        skip_open_minutes=int(r["skip_open_minutes"]),
        skip_close_minutes=int(r["skip_close_minutes"]),
    )
    costs = Costs(
        slippage_pct=float(raw["backtest"]["slippage_pct"]),
        spread_pct=float(raw["backtest"]["spread_pct"]),
    )
    symbols = tuple(s.upper() for s in raw["symbols"])
    return Config(raw=raw, risk=risk, symbols=symbols, costs=costs)


def load_paper_keys() -> tuple[str, str]:
    """Read paper API keys from .env / environment."""
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    key = os.environ.get("ALPACA_PAPER_API_KEY", "")
    secret = os.environ.get("ALPACA_PAPER_SECRET_KEY", "")
    if not key or not secret or key.startswith("your_"):
        raise ConfigError(
            "Missing Alpaca paper keys. Copy .env.example to .env and fill in "
            "ALPACA_PAPER_API_KEY and ALPACA_PAPER_SECRET_KEY."
        )
    return key, secret
