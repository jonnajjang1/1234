import math
import time
import logging
from collections import deque
from typing import Dict, Tuple, Optional, List
from core_constants import *

class StrategyLogic:
    """
    Shark-Pulse V58.0 Core Logic
    Refactored: Divergence & Acceleration removed.
    Efficiency merged into Friction as unified Absorption Power.
    Squeeze converted to sigmoid continuous function.
    """
    persistence_history: Dict[str, dict] = {}
    config: Optional[dict] = None
    last_market_data: Optional[dict] = None
    dynamic_threshold: float = SCORE_THRESHOLD_DEFAULT
    global_volatility: float = 1.0
    market_median_vwap: float = 0.0  # [V61.3 FIX] Macro Noise Filter
    nfe_matrix_long = {}
    nfe_matrix_short = {}
    oi_matrix = {}

    # --- [V61.0] REGIME-ADAPTIVE SCALING (SMART GEARBOX) ---
    current_regime: str = "TREND"
    regime_last_change: float = 0
    regime_multipliers: dict = {'vwap_gate_mult': 1.0, 'score_th_mult': 1.0, 'trail_mult': 1.0}
    
    REGIME_CONFIG = {
        'RANGE': {'vwap_gate_mult': 0.6,  'score_th_mult': 0.75, 'trail_mult': 0.7},
        'DRIFT': {'vwap_gate_mult': 0.75, 'score_th_mult': 0.85, 'trail_mult': 0.8},
        'TREND': {'vwap_gate_mult': 1.0,  'score_th_mult': 1.0,  'trail_mult': 1.0},
        'CHAOS': {'vwap_gate_mult': 1.25, 'score_th_mult': 1.2,  'trail_mult': 1.3},
    }

    @staticmethod
    def initialize(config: dict):
        StrategyLogic.config = config

    @staticmethod
    def update_market_context(market_data: dict):
        if not market_data: return
        
        # [P3-1] Single-pass: compute avg_abs_z AND directional consensus simultaneously.
        # Old code made 2 separate list/generator iterations over market_data.values()
        # plus a sum() pass over abs_zs — 3 passes total → 1 pass now.
        abs_z_sum = 0.0; n_valid = 0; positive = 0
        for d in market_data.values():
            vz = d.get('vwap_z')
            if vz is None: continue
            abs_z_sum += abs(vz)
            n_valid += 1
            if vz > 0.5: positive += 1
        avg_abs_z = abs_z_sum / n_valid if n_valid > 0 else 1.0
        StrategyLogic.global_volatility = avg_abs_z

        # [V61.3 FIX] Macro Noise Filtering (Beta Neutralization)
        # Calculate the median VWAP_Z across the market to subtract from individual scores
        vwap_z_list = sorted([d.get('vwap_z', 0.0) for d in market_data.values() if d.get('vwap_z') is not None])
        if vwap_z_list:
            mid = len(vwap_z_list) // 2
            StrategyLogic.market_median_vwap = (vwap_z_list[mid] + vwap_z_list[~mid]) / 2.0
        else:
            StrategyLogic.market_median_vwap = 0.0

        total = len(market_data)
        consensus = abs(positive / total - 0.5) * 2.0 if total > 0 else 0.5  # 0=ranging, 1=trending
        
        # 3. Regime Classification
        is_high_vol = avg_abs_z > 1.2
        is_high_con = consensus > 0.6
        
        new_regime = "TREND"
        if is_high_vol: new_regime = "TREND" if is_high_con else "CHAOS"
        else: new_regime = "DRIFT" if is_high_con else "RANGE"
        
        # 4. Hysteresis & State Update (60s holdoff)
        now = time.time()
        if new_regime != StrategyLogic.current_regime:
            if now - StrategyLogic.regime_last_change > 60.0:
                logging.info(f"🚦 [REGIME] Shift: {StrategyLogic.current_regime} -> {new_regime} (Vol:{avg_abs_z:.2f}, Con:{consensus:.2f})")
                StrategyLogic.current_regime = new_regime
                StrategyLogic.regime_last_change = now
                StrategyLogic.regime_multipliers = StrategyLogic.REGIME_CONFIG[new_regime]

        # Base Threshold Calculation
        deviation = (avg_abs_z - 1.0) * (1.0 / VOL_SENSITIVITY)
        new_th = SCORE_THRESHOLD_DEFAULT + (math.tanh(deviation * 0.5) * 150.0)
        StrategyLogic.dynamic_threshold = max(SCORE_THRESHOLD_MIN, min(SCORE_THRESHOLD_MAX, new_th))

    @staticmethod
    def calculate_hybrid_score(symbol: str, data: dict, intel, rank_maps: dict = None, is_active_position: bool = False) -> Tuple[float, str, str, str, dict]:
        m = StrategyLogic._extract_metrics(data, intel)
        m['symbol'] = symbol 
        if m['rsi5'] == 0: return 0.0, "WARMUP", "NONE", "NONE", {}
        
        # [V61.3 FIX] Apply Macro Noise Filtering: Subtract market median VWAP
        m['vwap_raw'] = m['vwap'] # Backup original for debugging/logging
        m['vwap'] = m['vwap'] - StrategyLogic.market_median_vwap
        
        # [V61.0] Apply Regime Scaling
        # [P0-5b FIX] Removed duplicate block (copy-paste artifact).
        rm = StrategyLogic.regime_multipliers
        scaled_th = StrategyLogic.dynamic_threshold * rm['score_th_mult']
        scaled_vwap_l = GATE_VWAP_LONG * rm['vwap_gate_mult']
        scaled_vwap_s = GATE_VWAP_SHORT * rm['vwap_gate_mult']
        
        hist = StrategyLogic._get_symbol_history(symbol)
        last_rsi = hist.get('last_rsi', 50.0); rsi_delta = m['rsi5'] - last_rsi; hist['last_rsi'] = m['rsi5']
        
        # [V60.4] VWAP Acceleration
        last_vwap_z = hist.get('last_vwap_z', 0.0); vwap_vel = abs(m['vwap']) - abs(last_vwap_z); hist['last_vwap_z'] = m['vwap']
        last_vwap_vel = hist.get('last_vwap_vel', 0.0); m['vwap_acc'] = vwap_vel - last_vwap_vel; hist['last_vwap_vel'] = vwap_vel
        
        # --- OI DECOUPLING SNAPSHOTS (V58.0 Patch) ---
        now_ts = time.time()
        if not hist['oi_snapshots'] or (now_ts - hist['oi_snapshots'][-1][0] >= OI_DECOUPLE_INTERVAL):
            hist['oi_snapshots'].append((now_ts, m['cp'], m['oi_raw']))
        
        # --- [V61.4 FIX] DUAL-ZONE LOB ABSORPTION ---
        # Map front_rep_z to legacy absorption_power for backwards compatibility in scoring
        m['absorption_power'] = max(0.0, m.get('front_rep_z', 0.0))
        
        last_abs = hist.get('last_abs', 0.0)
        m['abs_vel'] = m['absorption_power'] - last_abs
        hist['last_abs'] = m['absorption_power']
        
        m['is_recoiling'] = (abs(m['vwap']) > 2.2 and vwap_vel < -0.02)
        # [V61.4 FIX] Perfect Fit: True physical absorption based on Front Replenish
        m['is_absorbed'] = (m.get('front_rep_z', 0.0) > 1.5)
        
        # --- SIGMOID SQUEEZE (V58.0 Continuous) ---
        last_price = hist.get('last_price', m['cp']); last_oi = hist.get('last_oi', m['oi_raw'])
        price_chg = abs(m['cp'] - last_price) / (last_price + 1e-9); oi_chg = (m['oi_raw'] - last_oi) / (last_oi + 1e-9)
        hist['last_price'] = m['cp']; hist['last_oi'] = m['oi_raw']
        
        # [V60.4] OI Velocity (Directional)
        m['oi_vel'] = oi_chg
        
        squeeze_strength = oi_chg / (price_chg + 1e-4) - 1.0
        
        # [V58.0 Patch] Math Range Error Prevention
        # Clamp strength to prevent exp() overflow. 
        # Range -20 to 20 covers extreme scenarios safely.
        squeeze_strength = max(-20.0, min(20.0, squeeze_strength))
        
        squeeze_bonus = SQUEEZE_MAX_BONUS / (1.0 + math.exp(-SQUEEZE_SENSITIVITY * (squeeze_strength - SQUEEZE_MIDPOINT)))
        if squeeze_strength < 0: squeeze_bonus = 0.0

        # [V60.4] Liquidation Asymmetry Ratio
        m['liq_ratio'] = (m['l_liq_z'] - m['s_liq_z']) / (abs(m['l_liq_z']) + abs(m['s_liq_z']) + 0.1)

        nexus = StrategyLogic._calculate_nexus_metrics(hist, m)
        nfe_l, nfe_s = StrategyLogic._calculate_nfe_score(symbol, m, hist)
        
        StrategyLogic.nfe_matrix_long[symbol] = nfe_l; StrategyLogic.nfe_matrix_short[symbol] = nfe_s; StrategyLogic.oi_matrix[symbol] = max(0.0, m['oi_z'])
        
        l_rank = rank_maps['l_rank'].get(symbol, 999) if rank_maps else 999
        s_rank = rank_maps['s_rank'].get(symbol, 999) if rank_maps else 999
        oi_rank = rank_maps['oi_rank'].get(symbol, 999) if rank_maps else 999
            
        is_p_s, is_p_l = StrategyLogic._check_parabolic_state(m)
        mode, side, dtd_eff = StrategyLogic._determine_master_mode(m, l_rank, s_rank, oi_rank, is_p_s or is_p_l, rm['vwap_gate_mult'])
        sc_s, sc_l = StrategyLogic._calculate_scores(m, nexus, rsi_delta, vwap_vel, squeeze_bonus)
        
        # --- OI DECOUPLING LOGIC (Physics-based Distribution Check) ---
        decoupling_boost_s = 1.0
        decoupling_boost_l = 1.0

        if mode == "APEX-REVERSAL" and len(hist['oi_snapshots']) >= OI_DECOUPLE_WINDOW:
            past_ts, past_p, past_oi = hist['oi_snapshots'][0] 
            if past_p > 0 and past_oi > 0:
                oi_d = (m['oi_raw'] - past_oi) / past_oi
                p_d = (m['cp'] - past_p) / past_p
                
                if m['s_liq_z'] > 1.5 or m['l_liq_z'] > 1.5:
                    pass
                elif p_d > 0.001 and oi_d < -0.001:
                    boost = 1.0 + min(OI_DECOUPLE_CAP, abs(oi_d) * OI_DECOUPLE_SCALE)
                    decoupling_boost_s = boost
                elif p_d < -0.001 and oi_d < -0.001:
                    boost = 1.0 + min(OI_DECOUPLE_CAP, abs(oi_d) * OI_DECOUPLE_SCALE)
                    decoupling_boost_l = boost

        sc_s *= decoupling_boost_s
        sc_l *= decoupling_boost_l

        # --- V58.0 SYNERGY COUNT (Removed acc_z, added absorption) ---
        syn_count = 0
        if m['absorption_power'] > ABSORPTION_SYNERGY_LIMIT: syn_count += 1
        if abs(m['oi_z']) > SYNERGY_OI_LIMIT: syn_count += 1
        if abs(m['vwap']) > 2.0: syn_count += 1
        
        synergy_mult = 1.0 if syn_count < 2 else min(SYNERGY_MULT_MAX, SYNERGY_MULT_BASE * (1.15 ** (syn_count - 2)))
        sc_s *= synergy_mult; sc_l *= synergy_mult
        
        sc_s = StrategyLogic._apply_bonuses(sc_s, "SHORT", m, s_rank, oi_rank, mode)
        sc_l = StrategyLogic._apply_bonuses(sc_l, "LONG", m, l_rank, oi_rank, mode)
        
        f_s, f_l = StrategyLogic._apply_persistence(hist, sc_s, sc_l)
        
        # [V60.7 FIX] Auto-select dominant side for display if no mode is locked
        if side == "NONE":
            if f_s > f_l:
                f_score = f_s
                side = "SHORT"
            else:
                f_score = f_l
                side = "LONG"
        else:
            f_score = f_s if side == "SHORT" else f_l
        
        # --- PHASE 4: FULL DETAILS CONSTRUCTION (Moved Up) ---
        # Construct details BEFORE veto check to ensure active positions get data
        details = {
            'rsi5': m['rsi5'], 'rsi15': m['rsi15'], 'vwap': m['vwap'],
            'oi_z': m['oi_z'], 'cvd_z': m['cvd_z'], 'dtd': dtd_eff,
            'absorption': m.get('absorption_power', 0.0), # V58.0 Unified
            'fric': m['fric'],
            's_score': f_score, 
            'th': scaled_th,
            'mode': mode, 'ver': "V61.0", 'syn': syn_count,
            'rsi_delta': rsi_delta, 'vwap_vel': vwap_vel,
            'oi_raw': m['oi_raw'], 'squeeze': squeeze_bonus,
            'liq_ratio': m['liq_ratio'], 'abs_vel': m['abs_vel'],
            'regime': StrategyLogic.current_regime
        }        # [V61.2 FIX] Inject s_score into m so _check_vetoes can read it
        m['s_score'] = f_score
        veto = StrategyLogic._check_vetoes(m, is_p_s, is_p_l, mode, side)
        
        if veto: 
            return 0.0, veto, mode, side, details
            
        is_sniper = False
        if f_score >= scaled_th and mode != "NONE":
            vol_adj = max(0.6, min(1.8, StrategyLogic.global_volatility))
            if mode == "APEX-REVERSAL":
                if side == "SHORT":
                    is_vwap_gate = (m['vwap'] >= scaled_vwap_s)
                    is_rsi_gate = (m['rsi5'] >= (GATE_RSI_SHORT + (RSI_GATE_FLEX / vol_adj))) or (m['rsi5'] > GATE_RSI_PARABOLIC_UPPER)
                else:
                    is_vwap_gate = (m['vwap'] <= scaled_vwap_l)
                    is_rsi_gate = (m['rsi5'] <= (GATE_RSI_LONG - (RSI_GATE_FLEX / vol_adj))) or (m['rsi5'] < GATE_RSI_PARABOLIC_LOWER)
            else:
                is_vwap_gate = True
                is_rsi_gate = m['rsi5'] < 88.0 if side == "LONG" else m['rsi5'] > 12.0
            
            if is_vwap_gate and is_rsi_gate and syn_count >= 2: is_sniper = True
        
        # Optimize details for non-active, non-sniper (save SHM bandwidth?)
        # Actually, Python to Python dict passing is cheap. Keep full details.
        # But if we want to mimic old behavior for non-active:
        if not (is_sniper or f_score > 120 or is_active_position):
             details = {'s_score': f_score, 'mode': mode}
            
        return (f_score, "SNIPER", mode, side, details) if is_sniper else (0.0, "WAIT", mode, side, details)

    @staticmethod
    def _determine_master_mode(m, l_rk, s_rk, oi_rk, is_insane, vg_mult=1.0) -> Tuple[str, str, float]:
        is_turn_up = (m['vwap_raw'] < -1.5 and m['vel'] > 0.2 and m['cvd_z'] > 0.5)
        is_turn_down = (m['vwap_raw'] > 1.5 and m['vel'] < -0.2 and m['cvd_z'] < -0.5)
        # [V61.5 FIX] Bias must use vwap_raw to avoid distortion from Macro Noise Filter (Beta Neutralization)
        bias = "UP" if m['vwap_raw'] > 0 else "DOWN"
        mode, side = "NONE", "NONE"; dtd = abs(m['vwap']) / (abs(m['cvd_z']) + DTD_EPSILON); dtd_eff = math.tanh(dtd)
        is_whale = (oi_rk <= OI_RANK_LIMIT); is_rsi_ext = (m['rsi5'] > 75 or m['rsi5'] < 25); is_elastic = m.get('is_recoiling') or m.get('is_absorbed')
        
        # [V61.0] Scale Mode Activation Thresholds
        vwap_mode_th = 2.2 * vg_mult
        abs_mode_th = 2.0 * vg_mult
        
        if (is_insane or (abs(m['vwap']) >= vwap_mode_th and is_elastic and is_rsi_ext)) or (m.get('is_absorbed') and abs(m['vwap']) > abs_mode_th):
            mode = "APEX-REVERSAL"; side = "SHORT" if bias == "UP" else "LONG"
        elif is_whale:
            abs_pwr = m.get('absorption_power', 0.0)
            # [V60.2 FIX] Relaxed Whale Entry (oi_z 2.0 -> 0.5) to catch trends
            if abs(m['l_liq_z']) > WHALE_LIQ_Z_THRESHOLD or abs(m['s_liq_z']) > WHALE_LIQ_Z_THRESHOLD or (abs_pwr > 1.0 and m['oi_z'] > 0.5):
                 if m['oi_z'] > 0.2:
                     # [V61.2 FIX] WHALE-FORCE dead-zone mitigation
                     if abs(m['cvd_z']) >= 0.3:
                         mode = "WHALE-FORCE"; side = "LONG" if m['cvd_z'] > 0 else "SHORT" 
        return mode, side, dtd_eff

    @staticmethod
    def _extract_metrics(data: dict, intel) -> dict:
        def sv(v, d=0.0):
            try: return float(v) if math.isfinite(float(v)) else d
            except Exception: return d
        # [P3-3] Readability: split the one-liner return into aligned multi-line dict.
        return {
            'dr':      sv(data.get('depth_ratio', 0)),
            'rsi1':    sv(data.get('rsi1', 50)),
            'rsi5':    sv(data.get('rsi5', 50)),
            'rsi15':   sv(data.get('rsi15', 50)),
            'abs_sc':  sv(data.get('abs_score', 0)),
            'abs':     sv(data.get('abs_z', 0)),
            'vel':     sv(data.get('cvd_vel', 0)),
            'cvd_z':   sv(data.get('cvd_z', 0)),
            'acc_z':   sv(data.get('acc_z', 0)),
            'vwap':    max(-5.0, min(5.0, sv(data.get('vwap_z', 0.0)))),
            'eff':     sv(data.get('eff_z', 0)),
            'cp':      sv(data.get('cp', 0)),
            'oi_z':    sv(data.get('oi_z', 0.0)),
            'oi_raw':  sv(data.get('oi_raw', 0.0)),
            'fric':    sv(data.get('fric', 0.0)),
            's_low':   sv(data.get('s_low', 0.0)),
            's_high':  sv(data.get('s_high', 0.0)),
            'l_liq_z': sv(data.get('l_liq_z', 0.0)),
            's_liq_z': sv(data.get('s_liq_z', 0.0)),
            'ob_vel':  sv(data.get('ob_vel', 0.0)),
            'front_rep_z': sv(data.get('front_rep_z', 0.0)),
            'back_shift_z': sv(data.get('back_shift_z', 0.0)),
        }

    @staticmethod
    def _check_parabolic_state(m: dict) -> Tuple[bool, bool]:
        fuel = m['oi_z'] > 0.5; is_s = (m['rsi5'] > 85 and m['vwap'] > 3.5 and m['vel'] < 0 and fuel); is_l = (m['rsi5'] < 15 and m['vwap'] < -3.5 and m['vel'] > 0 and fuel)
        return is_s, is_l

    @staticmethod
    def _check_vetoes(m: dict, is_p_s: bool, is_p_l: bool, mode: str, side: str) -> Optional[str]:
        if m['cp'] <= 0: return "STALE_DATA_LOCK"
        # [V61.4 FIX] SPOOFING VETO (Trap Evasion - Falling Knife block)
        # If front is bleeding but back is padding fake walls
        if m.get('front_rep_z', 0.0) < 0 and m.get('back_shift_z', 0.0) > 1.0: return "SPOOFING_VETO"
        
        # [V60.2 FIX] Relaxed Wash Trading Veto (Virtually disabled unless blatant)
        if abs(m['vel']) > 0.99 and abs(m['ob_vel']) < 0.005: return "WASH_TRADING_VETO"
        climax = (m['rsi5'] > GATE_RSI_CLIMAX_UPPER or m['rsi5'] < GATE_RSI_CLIMAX_LOWER or abs(m['vwap']) > GATE_VWAP_CLIMAX)
        if mode != "WHALE-FORCE":
            if side == "LONG" and m['s_high'] > 0 and m['cp'] > m['s_high'] * TERRAIN_OFFSET_LONG: return "TERRAIN_RESISTANCE_VETO"
            if side == "SHORT" and m['s_low'] > 0 and m['cp'] < m['s_low'] * TERRAIN_OFFSET_SHORT: return "TERRAIN_SUPPORT_VETO"
        if not (is_p_s or is_p_l or climax or mode == "WHALE-FORCE"):
            if side == "LONG" and not (m['rsi5'] < 55 and m['rsi15'] < 60): return "MTF_DISALIGN_L"
            if side == "SHORT" and not (m['rsi5'] > 45 and m['rsi15'] > 40): return "MTF_DISALIGN_S"
        if mode == "WHALE-FORCE" and m['oi_z'] < 0.2: return "OI_OUTFLOW_VETO"
        # [V61.1 FIX] Mitigate OI_TOO_WEAK block during 429 stale periods. High scores bypass this.
        if abs(m['oi_z']) < 0.5 and not climax and mode == "NONE" and m.get('s_score', 0) < 200.0: return "OI_TOO_WEAK" 
        return None

    @staticmethod
    def _calculate_scores(m: dict, nexus: dict, rsi_delta: float, vwap_vel: float, squeeze_bonus: float) -> Tuple[float, float]:
        dr_p = (math.tanh(m['dr'] * 2.0) * 15.0) * nexus['w_integrity']
        ke_s, ke_l = m['vwap'] * 40.0, -m['vwap'] * 40.0
        rsi_h = {'s': max(0.0, m['rsi5'] - 65.0) * 18.0, 'l': max(0.0, 35.0 - m['rsi5']) * 18.0}
        abs_bonus = min(25.0, m['abs_sc'] * 6.0) if m['abs_sc'] > 0.8 else 0.0
        impulse_s = max(0.0, -rsi_delta * 2.0); impulse_l = max(0.0, rsi_delta * 2.0)
        
        # --- V58.0 UNIFIED ABSORPTION CONTRIBUTION ---
        abs_pwr = m.get('absorption_power', 0.0)
        if abs_pwr > 0.5 and abs(m['vwap']) > ABSORPTION_VWAP_GATE:
            vwap_intensity = min(2.0, abs(m['vwap']) / 2.0)
            raw_abs_score = abs_pwr * ABSORPTION_SCORE_SCALE * vwap_intensity
            abs_contribution = min(ABSORPTION_SCORE_CAP, raw_abs_score)
        else: abs_contribution = 0.0
        
        recoil = 30.0 if m.get('is_recoiling') else 0.0
        abs_s = (abs_contribution + recoil) if m['vwap'] > 0 else 0.0
        abs_l = (abs_contribution + recoil) if m['vwap'] < 0 else 0.0
        
        # [V60.4] Absorption Velocity Bonus
        # If wall is growing (abs_vel > 0), increase confidence. If collapsing, reduce.
        abs_vel_bonus = max(-20.0, min(20.0, m['abs_vel'] * 10.0))
        
        # [V60.5 FIX] Derive direction from VWAP (side is not available here)
        if m['vwap'] > 0: abs_s += abs_vel_bonus # Price High -> Wall supports Short Reversal
        elif m['vwap'] < 0: abs_l += abs_vel_bonus # Price Low -> Wall supports Long Reversal
        
        # Base Assembly (No Divergence)
        base_s = (ke_s + rsi_h['s'] - dr_p + abs_bonus + impulse_s + abs_s + squeeze_bonus)
        base_l = (ke_l + rsi_h['l'] + dr_p + abs_bonus + impulse_l + abs_l + squeeze_bonus)
        
        # [V60.4] Liquidation Asymmetry Ratio Multiplier (Continuous)
        liq_mult_s = 1.0 + max(0.0, m['liq_ratio'] * 0.5) 
        liq_mult_l = 1.0 + max(0.0, -m['liq_ratio'] * 0.5)
        
        base_s *= liq_mult_s
        base_l *= liq_mult_l

        # [V60.5 RESTORE] Extreme Liquidation Safety (Threshold Logic)
        # Prevent catching a falling knife if liquidation is EXTREME (> 2.0)
        s_liq, l_liq = m['s_liq_z'] > 2.0, m['l_liq_z'] > 2.0
        if s_liq and not l_liq:
            # Short Squeeze -> Don't Short! Boost Long.
            if m['cvd_z'] > 0.5: base_l += 30.0; base_s *= 0.2
        elif l_liq and not s_liq:
            # Long Squeeze -> Don't Long! Boost Short.
            if m['cvd_z'] < -0.5: base_s += 30.0; base_l *= 0.2

        return max(0.1, base_s), max(0.1, base_l)

    @staticmethod
    def _apply_bonuses(sc: float, side: str, m: dict, n_rk: int, o_rk: int, mode: str) -> float:
        if sc <= 0: return 0.0
        r_mult = max(1.0, 1.35 - (n_rk * 0.03)) if n_rk <= 15 else 1.0; o_mult = 1.25 if o_rk <= 20 else 1.0; mode_bonus = 1.2 if mode != "NONE" else 1.0
        return sc * r_mult * o_mult * mode_bonus

    @staticmethod
    def _calculate_nfe_score(sym: str, m: dict, hist: dict) -> Tuple[float, float]:
        try:
            if abs(m['vel']) < 0.05: return 0.0, 0.0
            eff_s, eff_l = math.tanh(max(0, m['cvd_z'])) ** 2, math.tanh(abs(min(0, m['cvd_z']))) ** 2
            nfe_s, nfe_l = (eff_s / (abs(m['vel']) + 0.01)) * 100.0, (eff_l / (abs(m['vel']) + 0.01)) * 100.0
            p_boost = math.exp(min(NFE_BOOST_CAP, abs(m['vwap']) / NFE_BOOST_SCALING))
            return nfe_l * p_boost, nfe_s * p_boost
        except Exception as e:
            logging.debug(f"NFE calc failed [{sym}]: {e}")
            return 0.0, 0.0

    @staticmethod
    def update_matrices_only(symbol: str, data: dict) -> Tuple[float, float]:
        """
        [P2-1] Lightweight Pass 1: updates NFE/OI matrices without full score computation.
        Does NOT mutate persistence_history — prevents double-update of hist state
        (old code zeroed rsi_delta/vwap_vel in Pass 2 because hist was already
        updated by Pass 1 on the same data dict).
        Returns (nfe_l, nfe_s).
        """
        def sv(v, d=0.0):
            try: return float(v) if math.isfinite(float(v)) else d
            except Exception: return d
        vel   = sv(data.get('cvd_vel', 0))
        cvd_z = sv(data.get('cvd_z', 0))
        vwap  = max(-5.0, min(5.0, sv(data.get('vwap_z', 0.0))))
        oi_z  = sv(data.get('oi_z', 0.0))
        nfe_l, nfe_s = 0.0, 0.0
        if abs(vel) >= 0.05:
            try:
                eff_s   = math.tanh(max(0, cvd_z)) ** 2
                eff_l   = math.tanh(abs(min(0, cvd_z))) ** 2
                p_boost = math.exp(min(NFE_BOOST_CAP, abs(vwap) / NFE_BOOST_SCALING))
                nfe_s   = (eff_s / (abs(vel) + 0.01)) * 100.0 * p_boost
                nfe_l   = (eff_l / (abs(vel) + 0.01)) * 100.0 * p_boost
            except Exception as e:
                logging.debug(f"NFE matrix update failed [{symbol}]: {e}")
        StrategyLogic.nfe_matrix_long[symbol]  = nfe_l
        StrategyLogic.nfe_matrix_short[symbol] = nfe_s
        StrategyLogic.oi_matrix[symbol]        = max(0.0, oi_z)
        return nfe_l, nfe_s

    @staticmethod
    def _get_symbol_history(symbol: str) -> dict:
        if symbol not in StrategyLogic.persistence_history:
            StrategyLogic.persistence_history[symbol] = {
                'SHORT': deque([0.0]*20, maxlen=20), 'LONG': deque([0.0]*20, maxlen=20),
                # [P1-1] Running sums for O(1) averaging in _apply_persistence.
                # Initial value is 0.0 because the deques start filled with 0.0.
                'sum_s': 0.0, 'sum_l': 0.0,
                'abs_streak': deque(maxlen=5), 'wall_window': deque(maxlen=3), 'oi_snapshots': deque(maxlen=12),
                'last_rsi': 50.0, 'last_vwap_z': 0.0, 'last_price': 0.0, 'last_oi': 0.0,
                'last_abs': 0.0, 'last_vwap_vel': 0.0
            }
        return StrategyLogic.persistence_history[symbol]

    @staticmethod
    def _apply_persistence(hist: dict, sc_s: float, sc_l: float) -> Tuple[float, float]:
        # [P1-1] O(1) running sum: subtract the value about to be evicted (index 0),
        # then append. Both deques are always full (init: [0.0]*20, maxlen=20),
        # so index[0] is always the next eviction candidate.
        hist['sum_s'] -= hist['SHORT'][0]
        hist['sum_l'] -= hist['LONG'][0]
        hist['SHORT'].append(sc_s)
        hist['LONG'].append(sc_l)
        hist['sum_s'] += sc_s
        hist['sum_l'] += sc_l
        avg_s = hist['sum_s'] / 20
        avg_l = hist['sum_l'] / 20
        return sc_s + (sc_s - avg_s) * 0.15, sc_l + (sc_l - avg_l) * 0.15

    @staticmethod
    def _calculate_nexus_metrics(hist: dict, m: dict) -> dict:
        now = time.time(); is_abs = (m['abs'] > 2.2 and m.get('absorption_power', 0) > 2.0)
        if is_abs: hist['abs_streak'].append(now)
        while hist['abs_streak'] and now - hist['abs_streak'][0] > 60: hist['abs_streak'].popleft()
        dr_p = (math.tanh(m['dr'] * 2.5) * 15.0); hist['wall_window'].append(dr_p); w_int = 1.0
        if len(hist['wall_window']) == 3 and abs(dr_p) > 2.5:
            mean = sum(hist['wall_window'])/3; std = math.sqrt(sum((x-mean)**2 for x in hist['wall_window'])/3)
            if std < (abs(dr_p) * 0.25): w_int = 1.25
        return {'f_locked': len(hist['abs_streak']) >= 3, 'w_integrity': w_int}

    @staticmethod
    def run_memory_gc(active_symbols: set):
        # [P3-2] O(k) in-place deletion vs O(n) full dict rebuild.
        # dict.keys() - set returns only stale symbols (k << n in steady state),
        # avoiding 4 new dict allocations and copies of all ~100 live entries.
        stale = StrategyLogic.persistence_history.keys() - active_symbols
        for s in stale:
            StrategyLogic.persistence_history.pop(s, None)
            StrategyLogic.nfe_matrix_long.pop(s, None)
            StrategyLogic.nfe_matrix_short.pop(s, None)
            StrategyLogic.oi_matrix.pop(s, None)
