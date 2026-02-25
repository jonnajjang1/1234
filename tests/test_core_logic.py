"""Tests for core_logic.py — scoring, vetoes, parabolic, bonuses, NFE, whale, persistence."""
import math
import time
import pytest
from collections import deque
from core_constants import *
from core_logic import StrategyLogic


@pytest.fixture(autouse=True)
def init_logic():
    """Reset StrategyLogic class state before each test."""
    StrategyLogic.initialize(CONFIG)
    StrategyLogic.persistence_history = {}
    StrategyLogic.nfe_matrix_long = {}
    StrategyLogic.nfe_matrix_short = {}
    StrategyLogic.oi_matrix = {}
    StrategyLogic.market_median_vwap = 0.0
    StrategyLogic.global_volatility = 1.0
    StrategyLogic.current_regime = "TREND"
    StrategyLogic.regime_multipliers = StrategyLogic.REGIME_CONFIG["TREND"]
    StrategyLogic.dynamic_threshold = SCORE_THRESHOLD_DEFAULT


# ─── _extract_metrics (sv / safe_float) ────────────────────────

class TestExtractMetrics:
    def test_normal_values(self):
        data = {'rsi5': 55.0, 'cp': 50000.0, 'vwap_z': 2.0, 'oi_z': 0.8}
        m = StrategyLogic._extract_metrics(data, None)
        assert m['rsi5'] == 55.0
        assert m['cp'] == 50000.0
        assert m['vwap'] == 2.0
        assert m['oi_z'] == 0.8

    def test_nan_becomes_zero(self):
        data = {'rsi5': float('nan'), 'cp': 100.0}
        m = StrategyLogic._extract_metrics(data, None)
        assert m['rsi5'] == 0.0

    def test_inf_becomes_zero(self):
        data = {'vwap_z': float('inf'), 'cp': 100.0}
        m = StrategyLogic._extract_metrics(data, None)
        assert m['vwap'] == 0.0

    def test_none_becomes_default(self):
        data = {'rsi5': None, 'cp': 100.0}
        m = StrategyLogic._extract_metrics(data, None)
        assert m['rsi5'] == 0.0

    def test_missing_keys_use_defaults(self):
        m = StrategyLogic._extract_metrics({}, None)
        assert m['rsi5'] == 50  # default from data.get('rsi5', 50)
        assert m['cp'] == 0
        assert m['oi_z'] == 0.0


# ─── _check_parabolic_state ─────────────────────────────────────

class TestParabolicState:
    def test_short_parabolic(self):
        m = {'oi_z': 1.0, 'rsi5': 90.0, 'vwap': 4.0, 'vel': -0.5}
        is_s, is_l = StrategyLogic._check_parabolic_state(m)
        assert is_s is True
        assert is_l is False

    def test_long_parabolic(self):
        m = {'oi_z': 1.0, 'rsi5': 10.0, 'vwap': -4.0, 'vel': 0.5}
        is_s, is_l = StrategyLogic._check_parabolic_state(m)
        assert is_s is False
        assert is_l is True

    def test_no_parabolic_low_fuel(self):
        m = {'oi_z': 0.3, 'rsi5': 90.0, 'vwap': 4.0, 'vel': -0.5}
        is_s, is_l = StrategyLogic._check_parabolic_state(m)
        assert is_s is False
        assert is_l is False

    def test_no_parabolic_neutral_rsi(self):
        m = {'oi_z': 1.0, 'rsi5': 50.0, 'vwap': 4.0, 'vel': -0.5}
        is_s, is_l = StrategyLogic._check_parabolic_state(m)
        assert is_s is False
        assert is_l is False


# ─── _check_vetoes ───────────────────────────────────────────────

class TestCheckVetoes:
    def _base_metrics(self):
        return {
            'cp': 100.0, 'rsi5': 50.0, 'rsi15': 50.0, 'vwap': 1.0,
            'vel': 0.1, 'ob_vel': 0.1, 's_high': 0.0, 's_low': 0.0,
            'oi_z': 1.0, 'cvd_z': 0.5, 'front_rep_z': 0.0, 'back_shift_z': 0.0,
            's_score': 300.0,
        }

    def test_stale_data_lock(self):
        m = self._base_metrics()
        m['cp'] = 0.0
        assert StrategyLogic._check_vetoes(m, False, False, "NONE", "NONE") == "STALE_DATA_LOCK"

    def test_spoofing_veto(self):
        m = self._base_metrics()
        m['front_rep_z'] = -1.0
        m['back_shift_z'] = 2.0
        assert StrategyLogic._check_vetoes(m, False, False, "NONE", "LONG") == "SPOOFING_VETO"

    def test_wash_trading_veto(self):
        m = self._base_metrics()
        m['vel'] = 1.0
        m['ob_vel'] = 0.001
        assert StrategyLogic._check_vetoes(m, False, False, "NONE", "LONG") == "WASH_TRADING_VETO"

    def test_terrain_resistance_long(self):
        m = self._base_metrics()
        m['s_high'] = 100.0
        m['cp'] = 100.3  # cp > s_high * 1.002
        assert StrategyLogic._check_vetoes(m, False, False, "APEX-REVERSAL", "LONG") == "TERRAIN_RESISTANCE_VETO"

    def test_terrain_support_short(self):
        m = self._base_metrics()
        m['s_low'] = 100.0
        m['cp'] = 99.7  # cp < s_low * 0.998
        assert StrategyLogic._check_vetoes(m, False, False, "APEX-REVERSAL", "SHORT") == "TERRAIN_SUPPORT_VETO"

    def test_mtf_disalign_long(self):
        m = self._base_metrics()
        m['rsi5'] = 60.0  # >= 55 → fails condition
        m['rsi15'] = 65.0
        assert StrategyLogic._check_vetoes(m, False, False, "NONE", "LONG") == "MTF_DISALIGN_L"

    def test_mtf_disalign_short(self):
        m = self._base_metrics()
        m['rsi5'] = 40.0  # <= 45 → fails condition
        m['rsi15'] = 35.0
        assert StrategyLogic._check_vetoes(m, False, False, "NONE", "SHORT") == "MTF_DISALIGN_S"

    def test_oi_outflow_veto(self):
        m = self._base_metrics()
        m['oi_z'] = 0.1  # < WHALE_MIN_OI_Z_FORCE
        assert StrategyLogic._check_vetoes(m, False, False, "WHALE-FORCE", "LONG") == "OI_OUTFLOW_VETO"

    def test_oi_too_weak(self):
        m = self._base_metrics()
        m['oi_z'] = 0.1
        m['s_score'] = 100.0
        assert StrategyLogic._check_vetoes(m, False, False, "NONE", "LONG") == "OI_TOO_WEAK"

    def test_no_veto_clean(self):
        m = self._base_metrics()
        assert StrategyLogic._check_vetoes(m, False, False, "APEX-REVERSAL", "LONG") is None

    def test_parabolic_bypasses_mtf(self):
        m = self._base_metrics()
        m['rsi5'] = 60.0  # Would trigger MTF_DISALIGN_L
        # But parabolic=True bypasses it
        assert StrategyLogic._check_vetoes(m, True, False, "APEX-REVERSAL", "LONG") is None

    def test_whale_bypasses_terrain(self):
        m = self._base_metrics()
        m['s_high'] = 100.0
        m['cp'] = 100.3
        # WHALE-FORCE mode bypasses terrain vetoes
        assert StrategyLogic._check_vetoes(m, False, False, "WHALE-FORCE", "LONG") is None


# ─── _calculate_scores ──────────────────────────────────────────

class TestCalculateScores:
    def _base_metrics(self):
        return {
            'dr': 0.5, 'vwap': 2.0, 'rsi5': 70.0, 'abs_sc': 1.5,
            'abs_z': 1.0, 'absorption_power': 2.0, 'is_recoiling': False,
            'abs_vel': 0.5, 'liq_ratio': 0.0, 's_liq_z': 0.5,
            'l_liq_z': 0.3, 'cvd_z': 0.2,
        }

    def test_returns_positive_scores(self):
        m = self._base_metrics()
        nexus = {'w_integrity': 1.0}
        sc_s, sc_l = StrategyLogic._calculate_scores(m, nexus, rsi_delta=5.0, squeeze_bonus=20.0)
        assert sc_s > 0.1
        # When vwap > 0, ke_l is negative → Long score can floor at 0.1
        assert sc_l >= 0.1

    def test_short_higher_when_vwap_positive(self):
        m = self._base_metrics()
        m['vwap'] = 3.0  # Strong positive → short signal
        m['rsi5'] = 75.0
        nexus = {'w_integrity': 1.0}
        sc_s, sc_l = StrategyLogic._calculate_scores(m, nexus, rsi_delta=0.0, squeeze_bonus=0.0)
        assert sc_s > sc_l

    def test_long_higher_when_vwap_negative(self):
        m = self._base_metrics()
        m['vwap'] = -3.0  # Strong negative → long signal
        m['rsi5'] = 25.0
        nexus = {'w_integrity': 1.0}
        sc_s, sc_l = StrategyLogic._calculate_scores(m, nexus, rsi_delta=0.0, squeeze_bonus=0.0)
        assert sc_l > sc_s

    def test_extreme_liq_suppresses_short(self):
        """Short squeeze (s_liq_z > 2) + cvd_z > 0.5 → Short *0.2, Long +30."""
        m = self._base_metrics()
        m['s_liq_z'] = 3.0
        m['l_liq_z'] = 0.1
        m['cvd_z'] = 1.0
        m['vwap'] = 2.0
        nexus = {'w_integrity': 1.0}

        # Get score WITHOUT extreme liq
        m_normal = {**m, 's_liq_z': 0.5}
        sc_s_normal, _ = StrategyLogic._calculate_scores(m_normal, nexus, rsi_delta=0.0, squeeze_bonus=0.0)

        # Get score WITH extreme liq
        sc_s_liq, sc_l_liq = StrategyLogic._calculate_scores(m, nexus, rsi_delta=0.0, squeeze_bonus=0.0)

        # Short should be suppressed relative to normal
        assert sc_s_liq < sc_s_normal

    def test_squeeze_bonus_directional(self):
        """Squeeze bonus only applies to reversal side."""
        m = self._base_metrics()
        m['vwap'] = 2.0
        nexus = {'w_integrity': 1.0}
        sc_s_with, _ = StrategyLogic._calculate_scores(m, nexus, 0.0, squeeze_bonus=25.0)
        sc_s_without, _ = StrategyLogic._calculate_scores(m, nexus, 0.0, squeeze_bonus=0.0)
        assert sc_s_with > sc_s_without  # vwap > 0 → squeeze_s gets the bonus

    def test_wall_integrity_amplifies_depth(self):
        """Higher wall integrity → bigger dr_p → affects Short score (dr_p subtracted from base_s)."""
        m = self._base_metrics()
        m['dr'] = 2.0
        nexus_high = {'w_integrity': 1.25}
        nexus_low = {'w_integrity': 1.0}
        sc_s_h, _ = StrategyLogic._calculate_scores(m, nexus_high, 0.0, 0.0)
        sc_s_l, _ = StrategyLogic._calculate_scores(m, nexus_low, 0.0, 0.0)
        # dr_p is subtracted from base_s, so higher integrity → lower Short score
        assert sc_s_h != sc_s_l


# ─── _apply_bonuses ──────────────────────────────────────────────

class TestApplyBonuses:
    def test_rank_multiplier(self):
        # n_rk=5 → r_mult = max(1.0, 1.35 - 0.03*5) = 1.20
        # o_rk=10 (<=20) → o_mult = 1.25
        # mode="APEX-REVERSAL" → mode_bonus = 1.2
        result = StrategyLogic._apply_bonuses(100.0, "LONG", {}, n_rk=5, o_rk=10, mode="APEX-REVERSAL")
        expected = 100.0 * 1.20 * 1.25 * 1.2
        assert abs(result - expected) < 0.01

    def test_no_bonus_high_rank(self):
        # n_rk=50 (>15) → r_mult = 1.0
        # o_rk=50 (>20) → o_mult = 1.0
        # mode="NONE" → mode_bonus = 1.0
        result = StrategyLogic._apply_bonuses(100.0, "LONG", {}, n_rk=50, o_rk=50, mode="NONE")
        assert abs(result - 100.0) < 0.01

    def test_zero_score(self):
        result = StrategyLogic._apply_bonuses(0.0, "SHORT", {}, n_rk=1, o_rk=1, mode="WHALE-FORCE")
        assert result == 0.0

    def test_negative_score(self):
        result = StrategyLogic._apply_bonuses(-10.0, "SHORT", {}, n_rk=1, o_rk=1, mode="NONE")
        assert result == 0.0


# ─── _calculate_nfe_score ────────────────────────────────────────

class TestNFEScore:
    def test_low_velocity_returns_zero(self):
        m = {'vel': 0.01, 'cvd_z': 1.0, 'vwap': 2.0}
        nfe_l, nfe_s = StrategyLogic._calculate_nfe_score("BTCUSDT", m, {})
        assert nfe_l == 0.0
        assert nfe_s == 0.0

    def test_positive_cvd_z_favors_short_nfe(self):
        m = {'vel': 0.5, 'cvd_z': 2.0, 'vwap': 1.0}
        nfe_l, nfe_s = StrategyLogic._calculate_nfe_score("BTCUSDT", m, {})
        # cvd_z > 0 → eff_s = tanh(2.0)^2 high, eff_l = tanh(0)^2 = 0
        assert nfe_s > nfe_l

    def test_negative_cvd_z_favors_long_nfe(self):
        m = {'vel': 0.5, 'cvd_z': -2.0, 'vwap': 1.0}
        nfe_l, nfe_s = StrategyLogic._calculate_nfe_score("BTCUSDT", m, {})
        assert nfe_l > nfe_s

    def test_higher_vwap_boosts_nfe(self):
        m1 = {'vel': 0.5, 'cvd_z': 1.5, 'vwap': 1.0}
        m2 = {'vel': 0.5, 'cvd_z': 1.5, 'vwap': 3.0}
        _, nfe_s1 = StrategyLogic._calculate_nfe_score("BTC", m1, {})
        _, nfe_s2 = StrategyLogic._calculate_nfe_score("BTC", m2, {})
        assert nfe_s2 > nfe_s1  # Higher vwap → higher p_boost


# ─── _calculate_whale_score ──────────────────────────────────────

class TestWhaleScore:
    def test_long_side(self):
        m = {
            'oi_z': 2.0, 's_liq_z': 2.5, 'l_liq_z': 0.5,
            'cvd_z': 1.5, 'vel': 0.8, 'absorption_power': 2.0,
        }
        sc_s, sc_l = StrategyLogic._calculate_whale_score(m, "LONG")
        assert sc_l > 100  # Should be a meaningful score
        assert sc_s == 0.1  # Opposite side gets floor

    def test_short_side(self):
        m = {
            'oi_z': 2.0, 's_liq_z': 0.5, 'l_liq_z': 2.5,
            'cvd_z': -1.5, 'vel': -0.8, 'absorption_power': 2.0,
        }
        sc_s, sc_l = StrategyLogic._calculate_whale_score(m, "SHORT")
        assert sc_s > 100
        assert sc_l == 0.1

    def test_low_oi_low_score(self):
        m = {
            'oi_z': 0.1, 's_liq_z': 0.0, 'l_liq_z': 0.0,
            'cvd_z': 0.0, 'vel': 0.0, 'absorption_power': 0.0,
        }
        sc_s, sc_l = StrategyLogic._calculate_whale_score(m, "LONG")
        assert sc_l < 20  # Very low score


# ─── _apply_persistence ──────────────────────────────────────────

class TestApplyPersistence:
    def _fresh_hist(self):
        return {
            'SHORT': deque([0.0] * 20, maxlen=20),
            'LONG': deque([0.0] * 20, maxlen=20),
            'sum_s': 0.0, 'sum_l': 0.0,
        }

    def test_cold_start_boost(self):
        """First score above 0 gets 15% momentum boost from zero-history."""
        hist = self._fresh_hist()
        f_s, f_l = StrategyLogic._apply_persistence(hist, 100.0, 100.0)
        # avg is (0*19 + 100)/20 = 5.0, boost = (100 - 5) * 0.15 = 14.25
        assert f_s > 100.0
        assert f_l > 100.0

    def test_stable_scores_converge(self):
        """After 20 identical scores, momentum boost → 0."""
        hist = self._fresh_hist()
        for _ in range(25):
            f_s, f_l = StrategyLogic._apply_persistence(hist, 200.0, 200.0)
        # After 20+ iterations of 200, avg ≈ 200, boost ≈ 0
        assert abs(f_s - 200.0) < 1.0

    def test_rising_scores_get_positive_boost(self):
        hist = self._fresh_hist()
        for i in range(20):
            f_s, _ = StrategyLogic._apply_persistence(hist, float(i * 10), 0.0)
        # Last score = 190, avg < 190, so boost is positive
        assert f_s > 190.0


# ─── _calculate_nexus_metrics ─────────────────────────────────────

class TestNexusMetrics:
    def test_first_call_returns_one(self):
        hist = {'wall_window': deque(maxlen=3)}
        m = {'dr': 2.0}
        nexus = StrategyLogic._calculate_nexus_metrics(hist, m)
        assert nexus['w_integrity'] == 1.0

    def test_consistent_walls_boost(self):
        hist = {'wall_window': deque(maxlen=3)}
        m = {'dr': 2.0}
        # Fill 3 consistent readings
        for _ in range(3):
            nexus = StrategyLogic._calculate_nexus_metrics(hist, m)
        # dr=2.0 → dr_p = tanh(5.0)*15 ≈ 14.99, abs > 2.5 → integrity check
        assert nexus['w_integrity'] >= 1.0


# ─── _determine_master_mode ──────────────────────────────────────

class TestDetermineMasterMode:
    def test_apex_reversal_short(self):
        m = {
            'vwap': 3.0, 'vwap_raw': 3.0, 'rsi5': 80.0, 'cvd_z': 0.5,
            'oi_z': 1.0, 'l_liq_z': 0.0, 's_liq_z': 0.0,
            'is_recoiling': True, 'is_absorbed': False,
            'absorption_power': 0.0, 'vel': 0.1,
        }
        mode, side, dtd = StrategyLogic._determine_master_mode(m, 50, 50, 50, False)
        assert mode == "APEX-REVERSAL"
        assert side == "SHORT"  # UP bias → SHORT reversal

    def test_apex_reversal_via_parabolic(self):
        m = {
            'vwap': 1.0, 'vwap_raw': 1.0, 'rsi5': 50.0, 'cvd_z': 0.5,
            'oi_z': 0.5, 'l_liq_z': 0.0, 's_liq_z': 0.0,
            'is_recoiling': False, 'is_absorbed': False,
            'absorption_power': 0.0, 'vel': 0.1,
        }
        # is_insane=True → always APEX
        mode, side, _ = StrategyLogic._determine_master_mode(m, 50, 50, 50, True)
        assert mode == "APEX-REVERSAL"

    def test_whale_force(self):
        m = {
            'vwap': 1.0, 'vwap_raw': 1.0, 'rsi5': 50.0, 'cvd_z': 1.0,
            'oi_z': 1.0, 'l_liq_z': 3.0, 's_liq_z': 0.5,
            'is_recoiling': False, 'is_absorbed': False,
            'absorption_power': 0.0, 'vel': 0.1,
        }
        # oi_rank <= OI_RANK_LIMIT → is_whale, l_liq_z > 2 → liq_triggered
        mode, side, _ = StrategyLogic._determine_master_mode(m, 50, 50, 10, False)
        assert mode == "WHALE-FORCE"
        assert side == "SHORT"  # l_liq_z > s_liq_z → SHORT

    def test_none_mode(self):
        m = {
            'vwap': 0.5, 'vwap_raw': 0.5, 'rsi5': 50.0, 'cvd_z': 0.5,
            'oi_z': 0.3, 'l_liq_z': 0.0, 's_liq_z': 0.0,
            'is_recoiling': False, 'is_absorbed': False,
            'absorption_power': 0.0, 'vel': 0.1,
        }
        mode, side, _ = StrategyLogic._determine_master_mode(m, 50, 50, 50, False)
        assert mode == "NONE"
        assert side == "NONE"


# ─── update_matrices_only ────────────────────────────────────────

class TestUpdateMatricesOnly:
    def test_updates_matrices(self):
        data = {'cvd_vel': 0.5, 'cvd_z': 1.0, 'vwap_z': 2.0, 'oi_z': 1.5}
        nfe_l, nfe_s = StrategyLogic.update_matrices_only("BTCUSDT", data)
        assert "BTCUSDT" in StrategyLogic.nfe_matrix_long
        assert "BTCUSDT" in StrategyLogic.nfe_matrix_short
        assert "BTCUSDT" in StrategyLogic.oi_matrix
        assert StrategyLogic.oi_matrix["BTCUSDT"] == 1.5

    def test_low_vel_zero_nfe(self):
        data = {'cvd_vel': 0.01, 'cvd_z': 1.0, 'vwap_z': 2.0, 'oi_z': 1.0}
        nfe_l, nfe_s = StrategyLogic.update_matrices_only("ETH", data)
        assert nfe_l == 0.0
        assert nfe_s == 0.0


# ─── update_market_context (regime classification) ────────────────

class TestUpdateMarketContext:
    def test_trend_regime(self):
        # High vol (avg_abs_z > 1.2) + high consensus (>0.6) → TREND
        market_data = {f"SYM{i}": {'vwap_z': 2.0} for i in range(10)}
        StrategyLogic.regime_last_change = 0  # Allow immediate change
        StrategyLogic.update_market_context(market_data)
        assert StrategyLogic.current_regime == "TREND"

    def test_range_regime(self):
        # Low vol (avg_abs_z <= 1.2) + low consensus (<=0.6) → RANGE
        # Need exactly half above 0.5, half below → consensus = 0
        StrategyLogic.current_regime = "TREND"
        StrategyLogic.regime_last_change = 0
        market_data = {}
        for i in range(20):
            # Half at +0.3 (below 0.5 threshold → not counted as positive)
            # Half at -0.3 → positive = 0, consensus = |0/20 - 0.5|*2 = 1.0
            # That's high consensus! We need ~50% above 0.5 for low consensus.
            # Use: 10 at +0.6 (positive), 10 at -0.6 → consensus = 0, avg_abs = 0.6
            market_data[f"SYM{i}"] = {'vwap_z': 0.6 if i % 2 == 0 else -0.6}
        StrategyLogic.update_market_context(market_data)
        # avg_abs_z = 0.6 (low vol), positive=10/20=50% → consensus = 0 (low)
        assert StrategyLogic.current_regime == "RANGE"

    def test_median_vwap_computed(self):
        market_data = {
            'A': {'vwap_z': 1.0}, 'B': {'vwap_z': 2.0},
            'C': {'vwap_z': 3.0}, 'D': {'vwap_z': 4.0},
        }
        StrategyLogic.update_market_context(market_data)
        # Sorted: [1,2,3,4], median = (2+3)/2 = 2.5
        assert abs(StrategyLogic.market_median_vwap - 2.5) < 0.01

    def test_dynamic_threshold_adjusts(self):
        old_th = StrategyLogic.dynamic_threshold
        market_data = {f"SYM{i}": {'vwap_z': 4.0} for i in range(10)}
        StrategyLogic.update_market_context(market_data)
        # High volatility → threshold should increase
        assert StrategyLogic.dynamic_threshold > old_th


# ─── run_memory_gc ───────────────────────────────────────────────

class TestMemoryGC:
    def test_gc_removes_stale_symbols(self):
        StrategyLogic.persistence_history = {'BTCUSDT': {}, 'STALE': {}}
        StrategyLogic.nfe_matrix_long = {'BTCUSDT': 1.0, 'STALE': 2.0}
        StrategyLogic.nfe_matrix_short = {'BTCUSDT': 1.0, 'STALE': 2.0}
        StrategyLogic.oi_matrix = {'BTCUSDT': 1.0, 'STALE': 2.0}

        StrategyLogic.run_memory_gc(active_symbols={'BTCUSDT'})

        assert 'STALE' not in StrategyLogic.persistence_history
        assert 'STALE' not in StrategyLogic.nfe_matrix_long
        assert 'BTCUSDT' in StrategyLogic.persistence_history
