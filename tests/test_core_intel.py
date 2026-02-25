"""Tests for core_intel.py — RSI calculation, rolling z-score stats, calibrated RSI."""
import pytest
import math
from core_intel import MarketIntelligence


@pytest.fixture
def intel():
    return MarketIntelligence()


# ─── _calculate_wilder_rsi ───────────────────────────────────────

class TestWilderRSI:
    def test_insufficient_data_returns_50(self, intel):
        prices = [100.0] * 10  # Only 10, need >= 15
        avg_up, avg_down = intel._calculate_wilder_rsi(prices, period=14)
        assert avg_up == 50.0
        assert avg_down == 50.0

    def test_known_calculation(self, intel):
        # Generate 20 prices with alternating up/down
        prices = [100.0]
        for i in range(19):
            prices.append(prices[-1] + (1.0 if i % 2 == 0 else -0.5))
        avg_up, avg_down = intel._calculate_wilder_rsi(prices, period=14)
        assert avg_up > 0
        assert avg_down > 0

    def test_all_up_returns_high_rsi(self, intel):
        prices = [100.0 + i for i in range(20)]  # Monotonically increasing
        avg_up, avg_down = intel._calculate_wilder_rsi(prices, period=14)
        # All moves are up → avg_down ≈ 0, returns (avg_up, 0.000001)
        rsi = intel._get_rsi_from_state(avg_up, avg_down)
        assert rsi > 99.0

    def test_all_down_returns_low_rsi(self, intel):
        prices = [200.0 - i for i in range(20)]  # Monotonically decreasing
        avg_up, avg_down = intel._calculate_wilder_rsi(prices, period=14)
        rsi = intel._get_rsi_from_state(avg_up, avg_down)
        assert rsi < 1.0

    def test_equal_moves_gives_rsi_50(self, intel):
        # Alternate +1, -1 → equal up/down
        prices = [100.0]
        for i in range(29):
            prices.append(prices[-1] + (1.0 if i % 2 == 0 else -1.0))
        avg_up, avg_down = intel._calculate_wilder_rsi(prices, period=14)
        rsi = intel._get_rsi_from_state(avg_up, avg_down)
        assert 45.0 < rsi < 55.0


# ─── _get_rsi_from_state ─────────────────────────────────────────

class TestGetRSIFromState:
    def test_zero_avg_down_returns_100(self, intel):
        assert intel._get_rsi_from_state(1.5, 0.0) == 100.0

    def test_zero_both_returns_50(self, intel):
        assert intel._get_rsi_from_state(0.0, 0.0) == 50.0

    def test_equal_returns_50(self, intel):
        rsi = intel._get_rsi_from_state(1.0, 1.0)
        assert abs(rsi - 50.0) < 0.01

    def test_double_up_gives_67(self, intel):
        rsi = intel._get_rsi_from_state(2.0, 1.0)
        # RS = 2.0, RSI = 100 - 100/3 = 66.67
        assert abs(rsi - 66.67) < 0.1

    def test_range_0_100(self, intel):
        for au in [0.001, 0.5, 1.0, 5.0, 100.0]:
            for ad in [0.001, 0.5, 1.0, 5.0, 100.0]:
                rsi = intel._get_rsi_from_state(au, ad)
                assert 0.0 <= rsi <= 100.0


# ─── get_calibrated_rsi ──────────────────────────────────────────

class TestCalibratedRSI:
    def test_no_state_returns_zero(self, intel):
        assert intel.get_calibrated_rsi("BTCUSDT", 50000.0) == 0.0

    def test_price_up_increases_rsi(self, intel):
        intel.rsi_history["BTCUSDT"] = {'last_close': 100.0, 'last_ts': 1000}
        intel.rsi_state["BTCUSDT"] = {'au': 1.0, 'ad': 1.0, 'lp': 100.0}
        rsi_neutral = intel.get_calibrated_rsi("BTCUSDT", 100.0)
        rsi_up = intel.get_calibrated_rsi("BTCUSDT", 105.0)
        assert rsi_up > rsi_neutral

    def test_price_down_decreases_rsi(self, intel):
        intel.rsi_history["BTCUSDT"] = {'last_close': 100.0, 'last_ts': 1000}
        intel.rsi_state["BTCUSDT"] = {'au': 1.0, 'ad': 1.0, 'lp': 100.0}
        rsi_neutral = intel.get_calibrated_rsi("BTCUSDT", 100.0)
        rsi_down = intel.get_calibrated_rsi("BTCUSDT", 95.0)
        assert rsi_down < rsi_neutral

    def test_rsi15_same_behavior(self, intel):
        intel.rsi15_history["ETH"] = {'last_close': 3000.0, 'last_ts': 1000}
        intel.rsi15_state["ETH"] = {'au': 1.0, 'ad': 1.0, 'lp': 3000.0}
        rsi = intel.get_calibrated_rsi15("ETH", 3100.0)
        assert rsi > 50.0


# ─── _update_cvd_stats ───────────────────────────────────────────

class TestCVDStats:
    def test_warmup_returns_zero(self, intel):
        for i in range(5):
            intel._update_cvd_stats("BTC", 100.0 + i)
        assert intel.cvd_z_cache["BTC"] == 0.0  # < 10 samples

    def test_z_score_after_warmup(self, intel):
        # Feed 15 samples
        for i in range(15):
            intel._update_cvd_stats("BTC", float(i))
        assert intel.cvd_z_cache["BTC"] != 0.0

    def test_outlier_gets_high_z(self, intel):
        # Feed 20 stable values then one outlier
        for i in range(20):
            intel._update_cvd_stats("BTC", 100.0)
        intel._update_cvd_stats("BTC", 200.0)
        assert intel.cvd_z_cache["BTC"] > 1.0

    def test_constant_values_z_zero(self, intel):
        for i in range(20):
            intel._update_cvd_stats("BTC", 50.0)
        # All same → std ≈ 0 → z = 0
        assert intel.cvd_z_cache["BTC"] == 0.0

    def test_multiple_symbols_independent(self, intel):
        for i in range(15):
            intel._update_cvd_stats("BTC", 100.0)
            intel._update_cvd_stats("ETH", 200.0)
        intel._update_cvd_stats("BTC", 200.0)  # Outlier for BTC only
        assert intel.cvd_z_cache["BTC"] > intel.cvd_z_cache["ETH"]


# ─── _update_oi_stats ────────────────────────────────────────────

class TestOIStats:
    def test_empty_symbol_ignored(self, intel):
        intel._update_oi_stats("", 100.0, ts=1.0)
        assert "" not in intel.oi_z_cache

    def test_duplicate_ts_ignored(self, intel):
        intel._update_oi_stats("BTC", 100.0, ts=1.0)
        intel._update_oi_stats("BTC", 200.0, ts=1.0)  # Same ts → skip
        assert len(intel.oi_history["BTC"]) == 1

    def test_z_after_3_samples(self, intel):
        for i in range(1, 5):
            intel._update_oi_stats("BTC", 100.0 + i * 10, ts=float(i))
        assert intel.oi_z_cache["BTC"] != 0.0

    def test_warmup_scaling(self, intel):
        """OI z-score has 1.5x multiplier and warmup scaling (min(1.0, h_len/20))."""
        for i in range(1, 6):
            intel._update_oi_stats("BTC", 100.0 + i, ts=float(i))
        z_early = intel.oi_z_cache["BTC"]

        for i in range(6, 25):
            intel._update_oi_stats("BTC", 100.0 + i, ts=float(i))
        z_late = intel.oi_z_cache["BTC"]
        # After warmup, scaling factor = 1.0 (full), so z changes differently
        assert z_early != z_late

    def test_oi_data_updated(self, intel):
        intel._update_oi_stats("BTC", 12345.0, ts=1.0)
        assert intel.oi_data["BTC"] == 12345.0


# ─── _update_liq_stats ───────────────────────────────────────────

class TestLiqStats:
    def test_warmup_returns_zero(self, intel):
        for i in range(3):
            intel._update_liq_stats("BTC", 1000.0, "LONG")
        assert intel.liq_z_cache["LONG"]["BTC"] == 0.0

    def test_z_after_5_samples(self, intel):
        for i in range(6):
            intel._update_liq_stats("BTC", 1000.0 + i * 500, "LONG")
        assert intel.liq_z_cache["LONG"]["BTC"] != 0.0

    def test_sides_independent(self, intel):
        for i in range(10):
            intel._update_liq_stats("BTC", 1000.0, "LONG")
            intel._update_liq_stats("BTC", 5000.0, "SHORT")
        intel._update_liq_stats("BTC", 10000.0, "LONG")  # Spike on LONG only
        assert abs(intel.liq_z_cache["LONG"]["BTC"]) > abs(intel.liq_z_cache["SHORT"]["BTC"])

    def test_maxlen_50(self, intel):
        for i in range(60):
            intel._update_liq_stats("BTC", float(i), "SHORT")
        assert len(intel.liq_history["SHORT"]["BTC"]) == 50


# ─── get_latest_metrics ──────────────────────────────────────────

class TestGetLatestMetrics:
    def test_empty_returns_zeros(self, intel):
        m = intel.get_latest_metrics("UNKNOWN")
        assert m['oi_z'] == 0.0
        assert m['cvd_z'] == 0.0

    def test_populated_returns_cached(self, intel):
        intel.oi_z_cache["BTC"] = 1.5
        intel.cvd_z_cache["BTC"] = -0.8
        intel.liq_z_cache["LONG"]["BTC"] = 0.3
        intel.liq_z_cache["SHORT"]["BTC"] = -0.2
        m = intel.get_latest_metrics("BTC")
        assert m['oi_z'] == 1.5
        assert m['cvd_z'] == -0.8
        assert m['liq_l'] == 0.3
        assert m['liq_s'] == -0.2
