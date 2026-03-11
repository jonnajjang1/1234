"""Tests for core_trader.py — PnL, _can_open, exit conditions."""
import time
import pytest
from unittest.mock import patch
from core_constants import *


def _make_trader():
    """Create SharkTrader without DB initialization (for pure logic tests)."""
    from core_trader import SharkTrader
    t = SharkTrader.__new__(SharkTrader)
    t.sys_cfg = {'max_positions': 5}
    t.tg_cfg = {}
    t.fee_rate = FEE_RATE_TOTAL
    t.margin_ratio = 0.1
    t.global_pnl_stop_limit = GLOBAL_PNL_STOP_LIMIT
    t.positions = {}
    t.wallet = {'balance': 10000.0}
    t.loss_streak = {}
    t.session = None
    return t


# ─── _calculate_net_pnl ──────────────────────────────────────────

class TestCalculateNetPnl:
    def test_long_profit(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'LONG', 'lev': 10}
        # 10% rise * 10x lev = 100%, minus 2-way fees
        pnl = t._calculate_net_pnl(pos, 110.0)
        expected = (0.10 * 10) - (FEE_RATE_TOTAL * 10 * 2)
        assert abs(pnl - expected) < 0.001

    def test_long_loss(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'LONG', 'lev': 10}
        pnl = t._calculate_net_pnl(pos, 95.0)
        expected = (-0.05 * 10) - (FEE_RATE_TOTAL * 10 * 2)
        assert abs(pnl - expected) < 0.001
        assert pnl < 0

    def test_short_profit(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'SHORT', 'lev': 10}
        pnl = t._calculate_net_pnl(pos, 90.0)
        expected = (0.10 * 10) - (FEE_RATE_TOTAL * 10 * 2)
        assert abs(pnl - expected) < 0.001

    def test_short_loss(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'SHORT', 'lev': 10}
        pnl = t._calculate_net_pnl(pos, 105.0)
        assert pnl < 0

    def test_zero_price_returns_zero(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'LONG', 'lev': 10}
        assert t._calculate_net_pnl(pos, 0.0) == 0.0

    def test_negative_price_returns_zero(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'LONG', 'lev': 10}
        assert t._calculate_net_pnl(pos, -5.0) == 0.0

    def test_default_leverage_10(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'LONG'}  # No 'lev' key
        pnl = t._calculate_net_pnl(pos, 101.0)
        expected = (0.01 * 10) - (FEE_RATE_TOTAL * 10 * 2)
        assert abs(pnl - expected) < 0.001

    def test_breakeven_is_negative_due_to_fees(self):
        t = _make_trader()
        pos = {'entry': 100.0, 'type': 'LONG', 'lev': 10}
        pnl = t._calculate_net_pnl(pos, 100.0)
        # 0% move → only fees → negative
        assert pnl < 0
        assert abs(pnl - (-FEE_RATE_TOTAL * 10 * 2)) < 0.001


# ─── _can_open ───────────────────────────────────────────────────

class TestCanOpen:
    def test_can_open_fresh(self):
        t = _make_trader()
        assert t._can_open("BTCUSDT") is True

    def test_symbol_already_open(self):
        t = _make_trader()
        t.positions["BTCUSDT"] = {'entry': 100.0}
        assert t._can_open("BTCUSDT") is False

    def test_max_positions_reached(self):
        t = _make_trader()
        for i in range(5):
            t.positions[f"SYM{i}USDT"] = {'entry': 100.0}
        assert t._can_open("NEWUSDT") is False

    def test_zero_balance(self):
        t = _make_trader()
        t.wallet['balance'] = 0.0
        assert t._can_open("BTCUSDT") is False

    def test_negative_balance(self):
        t = _make_trader()
        t.wallet['balance'] = -100.0
        assert t._can_open("BTCUSDT") is False

    def test_loss_streak_cooldown(self):
        t = _make_trader()
        t.loss_streak["BTCUSDT"] = {'count': 3, 'last_time': time.time()}
        assert t._can_open("BTCUSDT") is False

    def test_loss_streak_expired(self):
        t = _make_trader()
        t.loss_streak["BTCUSDT"] = {'count': 3, 'last_time': time.time() - 400}
        assert t._can_open("BTCUSDT") is True

    def test_loss_streak_count_2_ok(self):
        t = _make_trader()
        t.loss_streak["BTCUSDT"] = {'count': 2, 'last_time': time.time()}
        assert t._can_open("BTCUSDT") is True

    def test_other_symbol_streak_no_effect(self):
        t = _make_trader()
        t.loss_streak["ETHUSDT"] = {'count': 5, 'last_time': time.time()}
        assert t._can_open("BTCUSDT") is True


# ─── _evaluate_exit_conditions ────────────────────────────────────

class TestExitConditions:
    def _make_pos(self, type_='LONG', mode='APEX-REVERSAL', **overrides):
        pos = {
            'entry': 100.0, 'type': type_, 'lev': 10,
            'start_time': time.time() - 300,
            'details': {
                'vwap': 2.0, 'absorption': 100.0, 'mode': mode,
                'max_price': 100.0,
            },
        }
        pos['details'].update(overrides)
        return pos

    def _make_data(self, cp=100.0, **overrides):
        d = {
            'cp': cp, 'absorption_power': 30.0, 'cvd_vel': 0.1,
            's_liq_z': 0.0, 'l_liq_z': 0.0,
        }
        d.update(overrides)
        return d

    # Hard stop
    def test_hard_stop_loss(self):
        t = _make_trader()
        pos = self._make_pos()
        d = self._make_data(cp=94.0)
        net_pct = HARD_STOP_LOSS_NET - 0.01  # Below threshold
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct, 100)
        assert reason is not None
        assert "Hard Stop" in reason

    def test_above_hard_stop_no_exit(self):
        t = _make_trader()
        pos = self._make_pos()
        d = self._make_data()
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=-0.01, duration=100)
        assert reason is None or "Hard Stop" not in reason

    # Orphan cleanup
    def test_orphan_cleanup_mode_none(self):
        t = _make_trader()
        pos = self._make_pos(mode='NONE')
        d = self._make_data()
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=GRACE_PERIOD_SEC + 1)
        assert reason is not None
        assert "Orphan" in reason

    def test_orphan_within_grace_period(self):
        t = _make_trader()
        pos = self._make_pos(mode='NONE')
        d = self._make_data()
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=GRACE_PERIOD_SEC - 10)
        # Should NOT be orphan exited yet
        assert reason is None or "Orphan" not in reason

    # APEX target hit
    def test_apex_target_hit(self):
        t = _make_trader()
        # vwap_z_entry = 3.0, target_roi = 3.0 * APEX_TARGET_ROI_MULT = 0.012
        pos = self._make_pos(mode='APEX-REVERSAL', vwap=3.0)
        # raw_roi = (102 - 100) / 100 = 0.02, net_pct > 0.005
        d = self._make_data(cp=102.0)
        net_pct = 0.01
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct, 100)
        assert reason is not None
        assert "APEX Target" in reason

    def test_apex_target_not_reached(self):
        t = _make_trader()
        pos = self._make_pos(mode='APEX-REVERSAL', vwap=3.0)
        # raw_roi = (100.5 - 100) / 100 = 0.005 < 0.012 target
        d = self._make_data(cp=100.5)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=100)
        # Should not hit APEX target
        assert reason is None or "APEX Target" not in reason

    # Absorption decay
    def test_absorption_decay_exit(self):
        t = _make_trader()
        pos = self._make_pos(mode='APEX-REVERSAL', absorption=200.0)
        # curr_abs (10) < entry_abs (200) * 0.3 (60) → exit
        d = self._make_data(absorption_power=10.0)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.0, duration=120)
        assert reason is not None
        assert "Absorption" in reason

    def test_absorption_no_decay(self):
        t = _make_trader()
        pos = self._make_pos(mode='APEX-REVERSAL', absorption=100.0)
        d = self._make_data(absorption_power=80.0)  # 80 >= 100 * 0.3
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.0, duration=120)
        assert reason is None or "Absorption" not in reason

    # WHALE fuel exhaustion
    def test_whale_fuel_exhausted_long(self):
        t = _make_trader()
        pos = self._make_pos(type_='LONG', mode='WHALE-FORCE')
        # LONG fueled by s_liq_z (short liquidations)
        d = self._make_data(s_liq_z=WHALE_LIQ_EXHAUST_Z - 0.1)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=60)
        assert reason is not None
        assert "Fuel Exhausted" in reason

    def test_whale_fuel_ok(self):
        t = _make_trader()
        pos = self._make_pos(type_='LONG', mode='WHALE-FORCE')
        d = self._make_data(s_liq_z=2.0)  # Plenty of fuel
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=60)
        assert reason is None or "Fuel" not in reason

    # WHALE momentum reversal
    def test_whale_momentum_reversal_long(self):
        t = _make_trader()
        pos = self._make_pos(type_='LONG', mode='WHALE-FORCE')
        d = self._make_data(
            cvd_vel=-(WHALE_MOMENTUM_REVERSAL_VEL + 0.01),
            s_liq_z=2.0,  # Fuel still ok
        )
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=60)
        assert reason is not None
        assert "Momentum Reversal" in reason

    def test_whale_momentum_reversal_short(self):
        t = _make_trader()
        pos = self._make_pos(type_='SHORT', mode='WHALE-FORCE')
        d = self._make_data(
            cvd_vel=WHALE_MOMENTUM_REVERSAL_VEL + 0.01,
            l_liq_z=2.0,
        )
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.01, duration=60)
        assert reason is not None
        assert "Momentum Reversal" in reason

    # Trailing stop
    def test_trailing_stop_long(self):
        t = _make_trader()
        pos = self._make_pos(type_='LONG', mode='APEX-REVERSAL', max_price=110.0)
        # Drop from 110 to 105: dist = (110-105)/110 = 4.5% > TRAIL_DIST_DEFAULT (2%)
        d = self._make_data(cp=105.0)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.0, duration=100)
        assert reason is not None
        assert "Trailing Stop" in reason

    def test_trailing_stop_short(self):
        t = _make_trader()
        pos = self._make_pos(type_='SHORT', mode='APEX-REVERSAL', max_price=90.0)
        # Rise from 90 to 95: dist = (95-90)/90 = 5.5% > TRAIL_DIST_DEFAULT (2%)
        d = self._make_data(cp=95.0)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.0, duration=100)
        assert reason is not None
        assert "Trailing Stop" in reason

    def test_no_trail_within_dist(self):
        t = _make_trader()
        pos = self._make_pos(type_='LONG', mode='APEX-REVERSAL', max_price=100.5)
        d = self._make_data(cp=100.0)
        # dist = 0.5% < 2% default
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.0, duration=100)
        assert reason is None or "Trailing" not in reason

    # WHALE trailing distances
    def test_whale_tight_trail_high_profit(self):
        t = _make_trader()
        pos = self._make_pos(type_='LONG', mode='WHALE-FORCE', max_price=106.0)
        d = self._make_data(cp=105.5, s_liq_z=2.0)
        # net_pct > 5% → trail_dist = TRAIL_WHALE_V_HIGH (0.005)
        # dist = (106-105.5)/106 = 0.47% < 0.5% (barely within)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.06, duration=100)
        # This should be very close to triggering. Let's check:
        p_dist = (106.0 - 105.5) / 106.0  # ≈ 0.00472
        if p_dist > TRAIL_WHALE_V_HIGH:
            assert "Trailing" in reason
        else:
            assert reason is None or "Trailing" not in reason

    # Combination: hard stop overrides everything
    def test_hard_stop_overrides_all(self):
        t = _make_trader()
        pos = self._make_pos(mode='WHALE-FORCE')
        d = self._make_data(cp=80.0, s_liq_z=5.0)  # Even with good fuel
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=HARD_STOP_LOSS_NET - 0.01, duration=100)
        assert "Hard Stop" in reason

    # No exit
    def test_no_exit_normal_conditions(self):
        t = _make_trader()
        pos = self._make_pos(mode='APEX-REVERSAL', max_price=100.0)
        d = self._make_data(cp=100.0, absorption_power=80.0)
        reason = t._evaluate_exit_conditions("BTC", pos, d, net_pct=0.0, duration=100)
        assert reason is None
