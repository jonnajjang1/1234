import json
import os

BASE_DIR = os.environ.get('SHARK_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "shark_config.json")

def load_full_config():
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except Exception as e:
        import sys, logging
        logging.critical(f"❌ [FATAL] Failed to load shark_config.json: {e}. Exiting to prevent default fallback.")
        sys.exit(1)

# Single Source of Truth
CONFIG = load_full_config()

def _validate_config(cfg):
    """Warn on invalid config values. Does not crash — graceful degradation."""
    import logging
    _c = cfg.get('constants', {})
    _s = cfg.get('system', {})
    checks = [
        ('DEFAULT_LEVERAGE', _s, 1, 125, 10),
        ('HARD_STOP_LOSS_NET', _c, -1.0, 0, -0.055),
        ('FEE_RATE_TOTAL', _c, 0, 0.01, 0.0012),
        ('GRACE_PERIOD_SEC', _c, 0, 3600, 300),
    ]
    for key, section, lo, hi, default in checks:
        try:
            val = float(section.get(key, default))
            if not (lo <= val <= hi):
                logging.warning(f"Config '{key}' = {val} out of range [{lo}, {hi}]. Using default {default}.")
                section[key] = default
        except (TypeError, ValueError):
            logging.warning(f"Config '{key}' is not numeric. Using default {default}.")
            section[key] = default

_validate_config(CONFIG)
_c = CONFIG.get('constants', {})
_s = CONFIG.get('system', {})

# --- SYSTEM METADATA ---
SYSTEM_VERSION = _s.get('VERSION', "V58.0-HYPERNOVA")
SHM_PATH = _s.get('SHM_PATH', "/dev/shm/shark_shm_v60")

# --- SCORING THRESHOLDS ---
SCORE_THRESHOLD_DEFAULT = _c.get('SCORE_THRESHOLD_DEFAULT', 400.0)
SCORE_THRESHOLD_MIN = _c.get('SCORE_THRESHOLD_MIN', 200.0)
SCORE_THRESHOLD_MAX = _c.get('SCORE_THRESHOLD_MAX', 500.0)

# --- PHASE 3 ADAPTIVE GATES ---
VOL_SENSITIVITY = _c.get('VOL_SENSITIVITY', 0.15)
RSI_GATE_FLEX = _c.get('RSI_GATE_FLEX', 5.0)

# --- DTD (Delta-To-Distance) PHYSICS ---
DTD_EPSILON = _c.get('DTD_EPSILON', 0.5)

# --- NFE HYPER-ADAPTIVE PHYSICS ---
NFE_BOOST_SCALING = _c.get('NFE_BOOST_SCALING', 1.2)
NFE_BOOST_CAP = _c.get('NFE_BOOST_CAP', 2.2)
RANK_LIMIT_NFE_POOL = int(_c.get('RANK_LIMIT_NFE_POOL', 25))
RANK_LIMIT_OI_POOL = int(_c.get('RANK_LIMIT_OI_POOL', 40))
OI_RANK_LIMIT = int(_c.get('OI_RANK_LIMIT', 25))

# --- SYNERGY LIMITS (V58.0) ---
SYNERGY_OI_LIMIT = _c.get('SYNERGY_OI_LIMIT', 0.8)
SYNERGY_MULT_BASE = _c.get('SYNERGY_MULT_BASE', 1.5)
SYNERGY_MULT_MAX = _c.get('SYNERGY_MULT_MAX', 2.5)

# --- ENTRY GATES (APEX) ---
GATE_VWAP_SHORT = _c.get('GATE_VWAP_SHORT', 2.0)
GATE_VWAP_LONG = _c.get('GATE_VWAP_LONG', -1.5)
GATE_RSI_SHORT = _c.get('GATE_RSI_SHORT', 70.0)
GATE_RSI_LONG = _c.get('GATE_RSI_LONG', 30.0)
GATE_RSI_PARABOLIC_UPPER = _c.get('GATE_RSI_PARABOLIC_UPPER', 85.0)
GATE_RSI_PARABOLIC_LOWER = _c.get('GATE_RSI_PARABOLIC_LOWER', 15.0)
GATE_RSI_CLIMAX_UPPER = _c.get('GATE_RSI_CLIMAX_UPPER', 88.0)
GATE_RSI_CLIMAX_LOWER = _c.get('GATE_RSI_CLIMAX_LOWER', 12.0)
GATE_VWAP_CLIMAX = _c.get('GATE_VWAP_CLIMAX', 3.8)

# --- WHALE MODE PHYSICS ---
WHALE_MIN_OI_Z = _c.get('WHALE_MIN_OI_Z', 0.3)
WHALE_MIN_OI_Z_FORCE = _c.get('WHALE_MIN_OI_Z_FORCE', 0.4)
WHALE_LIQ_Z_THRESHOLD = _c.get('WHALE_LIQ_Z_THRESHOLD', 2.0)

# --- TERRAIN PHYSICS ---
TERRAIN_OFFSET_LONG = _c.get('TERRAIN_OFFSET_LONG', 1.002)
TERRAIN_OFFSET_SHORT = _c.get('TERRAIN_OFFSET_SHORT', 0.998)

# --- LOSS PROTECTION ---
LOSS_STREAK_THRESHOLD = _c.get('LOSS_STREAK_THRESHOLD', -0.005)
GLOBAL_PNL_STOP_LIMIT = _c.get('GLOBAL_PNL_STOP_LIMIT', -0.03)
HARD_STOP_LOSS_NET = _c.get('HARD_STOP_LOSS_NET', -0.055)

# --- EXIT TARGETS ---
APEX_TARGET_ROI_MULT = _c.get('APEX_TARGET_ROI_MULT', 0.004)
APEX_ABSORPTION_DECAY_RATE = _c.get('APEX_ABSORPTION_DECAY_RATE', 0.3)
WHALE_LIQ_EXHAUST_Z = _c.get('WHALE_LIQ_EXHAUST_Z', -0.5)
WHALE_MOMENTUM_REVERSAL_VEL = _c.get('WHALE_MOMENTUM_REVERSAL_VEL', 0.15)

# --- TRAILING DISTANCES ---
TRAIL_DIST_DEFAULT = _c.get('TRAIL_DIST_DEFAULT', 0.02)
TRAIL_APEX_TIGHT = _c.get('TRAIL_APEX_TIGHT', 0.01)
TRAIL_APEX_MID = _c.get('TRAIL_APEX_MID', 0.015)
TRAIL_WHALE_V_HIGH = _c.get('TRAIL_WHALE_V_HIGH', 0.005)
TRAIL_WHALE_HIGH = _c.get('TRAIL_WHALE_HIGH', 0.01)
TRAIL_WHALE_MID = _c.get('TRAIL_WHALE_MID', 0.015)
TRAIL_WHALE_INITIAL = _c.get('TRAIL_WHALE_INITIAL', 0.025)

# --- TRADER EXECUTION ---
DEFAULT_LEVERAGE = int(_s.get('DEFAULT_LEVERAGE', 10))
FEE_RATE_TOTAL = _c.get('FEE_RATE_TOTAL', 0.0012)
GRACE_PERIOD_SEC = int(_c.get('GRACE_PERIOD_SEC', 300))

# --- DYNAMIC SIZING ---
SIZING_HIGH_CONF_SCORE = _c.get('SIZING_HIGH_CONF_SCORE', 350.0)
SIZING_LOW_CONF_SCORE = _c.get('SIZING_LOW_CONF_SCORE', 240.0)
SIZING_HIGH_MULT = _c.get('SIZING_HIGH_MULT', 1.5)
SIZING_LOW_MULT = _c.get('SIZING_LOW_MULT', 0.5)

# --- INTERVALS & TIMEOUTS ---
STALE_CHECK_TIMEOUT = _c.get('STALE_CHECK_TIMEOUT', 7.0)
LOGIC_GC_INTERVAL = int(_c.get('LOGIC_GC_INTERVAL', 600))
DISCOVERY_INTERVAL = int(_c.get('DISCOVERY_INTERVAL', 300))
SCANNER_SLEEP_TICK = _c.get('SCANNER_SLEEP_TICK', 0.01)

# --- V58.0 HYPERNOVA REFACTOR CONSTANTS ---
ABSORPTION_EFF_DISCOUNT = _c.get('ABSORPTION_EFF_DISCOUNT', 0.3)
ABSORPTION_SYNERGY_LIMIT = _c.get('ABSORPTION_SYNERGY_LIMIT', 1.8)
SQUEEZE_MAX_BONUS = _c.get('SQUEEZE_MAX_BONUS', 30.0)
SQUEEZE_SENSITIVITY = _c.get('SQUEEZE_SENSITIVITY', 3.0)
SQUEEZE_MIDPOINT = _c.get('SQUEEZE_MIDPOINT', 1.5)
ABSORPTION_SCORE_SCALE = _c.get('ABSORPTION_SCORE_SCALE', 12.0)
ABSORPTION_SCORE_CAP = _c.get('ABSORPTION_SCORE_CAP', 80.0)
ABSORPTION_VWAP_GATE = _c.get('ABSORPTION_VWAP_GATE', 1.5)

# --- OI DECOUPLING PHYSICS (V58.0 Patch) ---
OI_DECOUPLE_SCALE = _c.get('OI_DECOUPLE_SCALE', 50.0)
OI_DECOUPLE_CAP = _c.get('OI_DECOUPLE_CAP', 1.5)
OI_DECOUPLE_WINDOW = int(_c.get('OI_DECOUPLE_WINDOW', 6))
OI_DECOUPLE_INTERVAL = _c.get('OI_DECOUPLE_INTERVAL', 5.0)
