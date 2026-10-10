from bot.strategies.base import Strategy, StrategyContext
from bot.strategies.faber import FaberStrategy
from bot.strategies.momentum import MomentumStrategy
from bot.strategies.rsi2 import RSI2Strategy

STRATEGIES: dict[str, type[Strategy]] = {
    MomentumStrategy.name: MomentumStrategy,
    FaberStrategy.name: FaberStrategy,
    RSI2Strategy.name: RSI2Strategy,
}


def build_strategy(name: str, params: dict) -> Strategy:
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy {name!r}; choose from {sorted(STRATEGIES)}")
    return STRATEGIES[name](params)


__all__ = ["Strategy", "StrategyContext", "STRATEGIES", "build_strategy"]
