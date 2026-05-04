from signals.donchian import DonchianBreakout
from signals.strategy import ThreeEMACross
from signals.volatility import VolatilityExpansion

_REGISTRY = {
    "three_ema_cross": ThreeEMACross,
    "donchian": DonchianBreakout,
    "volatility_expansion": VolatilityExpansion,
}


def get_strategy(
    coin: str, strategy_config: dict
) -> ThreeEMACross | DonchianBreakout | VolatilityExpansion:
    """Instantiate a strategy by name from strategy_config["type"]."""
    strategy_type = strategy_config.get("type", "three_ema_cross")
    cls = _REGISTRY.get(strategy_type)
    if cls is None:
        raise ValueError(f"Unknown strategy type '{strategy_type}'. Valid: {list(_REGISTRY)}")
    return cls(coin, strategy_config)
