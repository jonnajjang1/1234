"""Smoke test: verify modules load and basic math works."""


def test_config_loads():
    from core_constants import CONFIG, DEFAULT_LEVERAGE, BASE_DIR
    assert isinstance(CONFIG, dict)
    assert DEFAULT_LEVERAGE == 10
    assert BASE_DIR.endswith("1234")


def test_core_logic_imports():
    from core_logic import StrategyLogic
    from core_constants import CONFIG
    StrategyLogic.initialize(CONFIG)
    assert StrategyLogic.config is not None


def test_net_pnl_long():
    from core_trader import SharkTrader
    pos = {'entry': 100.0, 'type': 'LONG', 'lev': 10}
    trader = SharkTrader.__new__(SharkTrader)
    # 10% price rise * 10x lev = 100% gross, minus fees
    result = trader._calculate_net_pnl(pos, 110.0)
    assert result > 0.9  # ~1.0 - fees


def test_net_pnl_short():
    from core_trader import SharkTrader
    pos = {'entry': 100.0, 'type': 'SHORT', 'lev': 10}
    trader = SharkTrader.__new__(SharkTrader)
    # 10% price drop * 10x lev = 100% gross, minus fees
    result = trader._calculate_net_pnl(pos, 90.0)
    assert result > 0.9
