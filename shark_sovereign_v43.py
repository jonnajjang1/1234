import mmap; import struct; import time; import os; import json; import asyncio; import aiohttp; import logging; import fcntl; import signal; import sys; import re; import sqlite3; import subprocess; import ctypes; import importlib; import heapq; import shutil; import traceback
from datetime import datetime; from typing import Dict, List, Tuple, Optional; from collections import deque
BASE_DIR = "/home/ninano990707/shark_system"; sys.path.append(BASE_DIR)
from core_intel import MarketIntelligence; import core_trader; import core_logic; from core_constants import *

# [P0-5c FIX] Explicit mapping from data-dict keys to get_latest_metrics() keys.
# get_latest_metrics() returns: {oi_z, cvd_z, liq_l, liq_s}.
# The old one-liner used k.replace('_z','') which produced 'l_liq'/'s_liq'
# — keys that do NOT exist in i_m, so liq fallbacks silently returned 0.0.
_STALE_FALLBACK_KEY = {
    'oi_z':       'oi_z',
    'l_liq_z':    'liq_l',
    's_liq_z':    'liq_s',
    # acc_z / rejection_z / abs_z are C++-only metrics — not in i_m; 0.0 fallback is correct.
}

class SharedMetric(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("sequence", ctypes.c_uint64), ("price", ctypes.c_double), ("cvd_vel", ctypes.c_double), ("abs_score", ctypes.c_double), ("depth_ratio", ctypes.c_double), ("ob_vel", ctypes.c_double), ("rsi1", ctypes.c_double), ("rsi5", ctypes.c_double), ("rsi15", ctypes.c_double), ("vwap_z", ctypes.c_double), ("abs_z", ctypes.c_double), ("rejection_z", ctypes.c_double), ("eff_z", ctypes.c_double), ("vwap_raw", ctypes.c_double), ("cvd_total", ctypes.c_double), ("total_vol_raw", ctypes.c_double), ("oi_z", ctypes.c_double), ("l_liq_z", ctypes.c_double), ("s_liq_z", ctypes.c_double), ("liq_raw", ctypes.c_double), ("oi_raw", ctypes.c_double), ("acc_z", ctypes.c_double), ("score_short", ctypes.c_double), ("score_long", ctypes.c_double), ("front_rep_z", ctypes.c_double), ("back_shift_z", ctypes.c_double), ("win_delta_raw", ctypes.c_double),
        ("update_ms", ctypes.c_uint64), ("update_time", ctypes.c_int64), ("signal_code", ctypes.c_int32), ("signal_side", ctypes.c_int32), ("friction_val", ctypes.c_double), ("symbol", ctypes.c_char * 16), ("high15m", ctypes.c_double), ("low15m", ctypes.c_double)]

class SharedMemoryBlock(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("symbol_count", ctypes.c_int32), ("magic_number", ctypes.c_int32), ("padding", ctypes.c_char * 56), ("metrics", SharedMetric * 128)]

class SovereignEngine:
    def __init__(self):
        self.intel = MarketIntelligence(); self.trader = None; self.session = None; self.running = True; self.heartbeat_path = os.path.join(BASE_DIR, "logs/sovereign_heartbeat.ts"); self.market_data_cache = {}; self.last_seq = {}; self.startup_time = time.time(); self.last_logic_gc = time.time()

    async def start(self):
        try:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=50, keepalive_timeout=60))
            self.trader = core_trader.SharkTrader(CONFIG); self.trader.set_session(self.session)
            
            logging.info("🌟 Initializing Core Sub-tasks...")
            intel_task = asyncio.create_task(self.intel.run_intel_loop(self.session, logging.info))
            trader_task = asyncio.create_task(self.run_trader_loop())
            scanner_task = asyncio.create_task(self.run_scanner_loop())
            discovery_task = asyncio.create_task(self.run_discovery_task())
            
            # Wait for all tasks to run concurrently. If any task crashes, it will be caught here.
            await asyncio.gather(intel_task, trader_task, scanner_task, discovery_task)
            
        except Exception as e:
            logging.critical(f"💥 ENGINE CRITICAL FAILURE: {e}")
            logging.error(traceback.format_exc())
        finally:
            if self.session: await self.session.close()
            self.running = False

    async def run_discovery_task(self):
        logging.info("🔭 Discovery Task Started.")
        while self.running:
            try:
                # [V60.0 FIX] Dead Symbol Purge: Ensure trading status is populated first
                if not self.intel.trading_status:
                    await asyncio.sleep(5)
                    continue

                async with self.session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Strict Filtering: USDT only AND Status == TRADING AND Valid Format
                        valid_data = []
                        # [V60.0 ADJUST] Removed valid pairs (0G, PLAY, STABLE, PUMP) from blacklist
                        blacklist = {'VVVUSDT', 'XPLUSDT'} 
                        
                        for t in data:
                            sym = t['symbol']
                            if not sym.endswith('USDT'): continue
                            if self.intel.trading_status.get(sym) != 'TRADING': continue
                            if sym in blacklist: continue
                            if not re.match(r'^[A-Z0-9]{2,}USDT$', sym): continue # Reject Non-Alphanumeric (e.g. Chinese chars)
                            
                            valid_data.append(t)
                        
                        # Sort by Volume
                        top_100 = [t['symbol'] for t in sorted(valid_data, key=lambda x: float(x['quoteVolume']), reverse=True)[:100]]
                        
                        logging.info(f"🔄 Discovery: Found {len(top_100)} VALID top symbols (Filtered Dead Pairs).")
                        
                        cfg_path = os.path.join(BASE_DIR, "shark_config.json")
                        with open(cfg_path, 'r') as f: cfg = json.load(f)
                        
                        current_keys = set(cfg['symbols'].keys())
                        new_keys = set(top_100)
                        
                        logging.info(f"🔍 Discovery Debug: Current Keys={len(current_keys)}, New Keys={len(new_keys)}")
                        
                        if current_keys != new_keys:
                            logging.info("♻️ Config Mismatch Detected. Updating...")
                            cfg['symbols'] = {s: 100000.0 for s in top_100}
                            # [P0-4 FIX] Atomic write via temp file + os.replace().
                            # A direct open(..., 'w') write leaves a window where
                            # the C++ engine may read a partially-written JSON file,
                            # causing a parse failure and a missed reload.
                            # os.replace() is POSIX-atomic (single syscall rename).
                            tmp_path = cfg_path + ".tmp"
                            with open(tmp_path, 'w') as f:
                                json.dump(cfg, f, indent=4)
                                f.flush()
                                os.fsync(f.fileno())
                            shutil.copy2(cfg_path, cfg_path + ".bak")
                            os.replace(tmp_path, cfg_path)
                            logging.info("💾 Config Saved. Triggering Rotation...")
                            subprocess.run(["pkill", "-SIGUSR1", "-f", "shark_engine_v37"], check=False)
                            logging.info("✅ Signal Sent.")
                        else:
                            logging.info("💤 Config matches. No rotation needed.")
            except Exception as e: logging.error(f"Discovery Error: {e}")
            await asyncio.sleep(DISCOVERY_INTERVAL)

    async def run_trader_loop(self):
        while self.running:
            try:
                if self.market_data_cache:
                    pnl_res = await self.trader.update_pnl(self.market_data_cache, self.intel, time.time())
                    for sym, price, reason in pnl_res[2]: 
                        logging.info(f"🚪 EXIT TRIGGERED: {sym} for {reason}")
                        await self.trader.close_position(sym, price, reason, self.market_data_cache.get(sym))
                await asyncio.sleep(SCANNER_SLEEP_TICK)
            except Exception as e: logging.error(f"Trader Loop Error: {e}")

    async def run_scanner_loop(self):
        shm_path = SHM_PATH 
        while not os.path.exists(shm_path): await asyncio.sleep(1)
        
        metric_size = ctypes.sizeof(SharedMetric)
        fd = os.open(shm_path, os.O_RDWR); mm = mmap.mmap(fd, ctypes.sizeof(SharedMemoryBlock), mmap.MAP_SHARED); shm = SharedMemoryBlock.from_buffer(mm)
        
        symbol_cache = {}; intel = self.intel; logic = core_logic.StrategyLogic
        last_pulse_time = 0
        
        # --- [LOG_EXT] INTEGRITY & AUDIT STATS ---
        stats = {'shm_retries': 0, 'flash_vetos': 0, 'gate_rejections': 0, 'veto_rejections': 0}
        
        logging.info("🔬 Scanner Loop Starting (Zero-Lag Audit Mode)...")
        
        while self.running:
            try:
                start = time.perf_counter(); now_ts = time.time(); 
                with open(self.heartbeat_path, "w") as f: f.write(str(now_ts))
                
                if now_ts - last_pulse_time > 30:
                    logging.info(f"💓 [HEARTBEAT] Monitor: {shm.symbol_count} syms | SHM Retries: {stats['shm_retries']} | Flash Vetos: {stats['flash_vetos']} | Rejections: G:{stats['gate_rejections']} V:{stats['veto_rejections']}")
                    last_pulse_time = now_ts; stats = {k: 0 for k in stats} 
                
                intel.active_position_symbols = set(self.trader.positions.keys())
                
                # --- [V60.7] ZERO-LAG RANKING SYSTEM (2-PASS SCAN) ---
                seen_syms = set(); results = []; scan_queue = []
                loop_count = min(shm.symbol_count, 128)
                stale_engine_count = 0
                
                # PASS 1: GATHER RAW METRICS & UPDATE NFE MATRICES
                for i in range(loop_count):
                    try:
                        m = shm.metrics[i]
                        if m.update_time > 0 and now_ts - m.update_time > 15.0: stale_engine_count += 1
                        
                        success = False
                        for retry in range(3):
                            s1 = m.sequence
                            sym_pre = m.symbol.decode('ascii', errors='ignore').strip('\x00')
                            if s1 % 2 == 0 and sym_pre:
                                p, cv, dr, r1, r5, r15, vz, az, rz, ez, oz, acc, ll, sl, ut, fric, h15, l15 = m.price, m.cvd_vel, m.depth_ratio, m.rsi1, m.rsi5, m.rsi15, m.vwap_z, m.abs_z, m.rejection_z, m.eff_z, m.oi_z, m.acc_z, m.l_liq_z, m.s_liq_z, m.update_time, m.friction_val, m.high15m, m.low15m
                                sym_post = m.symbol.decode('ascii', errors='ignore').strip('\x00')
                                if m.sequence == s1 and sym_pre == sym_post:
                                    sym = sym_pre; success = True; break
                            # [P1-4] Removed await asyncio.sleep(0) from retry body.
                            # Yielding to the event loop on every SHM contention attempt
                            # (up to 3 retries × 128 symbols = 384 yields per scan tick)
                            # floods the scheduler. C++ writes complete in microseconds,
                            # so an immediate retry succeeds in practice. The scanner
                            # yields naturally via the final await asyncio.sleep() each tick.
                            if retry > 0: stats['shm_retries'] += 1
                        
                        if not success or p <= 0: continue
                        if sym in self.market_data_cache:
                            if abs(p - self.market_data_cache[sym]['cp']) / self.market_data_cache[sym]['cp'] > 0.5:
                                stats['flash_vetos'] += 1; continue
                        
                        if s1 > self.last_seq.get(sym, 0): intel._update_cvd_stats(sym, m.win_delta_raw); self.last_seq[sym] = s1
                        
                        r5_calib = intel.get_calibrated_rsi(sym, p); r15_calib = intel.get_calibrated_rsi15(sym, p)
                        if r5_calib == 0.0: r5_calib = r5 if r5 > 0 else 50.0
                        if r15_calib == 0.0: r15_calib = r15 if r15 > 0 else 50.0

                        i_m = intel.get_latest_metrics(sym)
                        data = {
                            'symbol': sym, 'cp': p, 'cvd_vel': cv, 'cvd_z': i_m['cvd_z'], 'depth_ratio': dr, 
                            'rsi1': r1, 'rsi5': r5_calib, 'rsi15': r15_calib, 
                            'vwap_z': vz, 'abs_z': az, 'rejection_z': rz, 'eff_z': ez, 'oi_z': oz, 
                            'acc_z': acc, 'l_liq_z': ll, 's_liq_z': sl, 'fric': fric, 
                            'oi_raw': intel.oi_data.get(sym, 0.0), 'ob_vel': m.ob_vel,
                            'front_rep_z': m.front_rep_z, 'back_shift_z': m.back_shift_z,
                            's_low': l15 if l15 > 0 else intel.struct_low_15m.get(sym, 0.0), 
                            's_high': h15 if h15 > 0 else intel.struct_high_15m.get(sym, 0.0)
                        }
                        for k in ['oi_z', 'acc_z', 'rejection_z', 'abs_z', 'l_liq_z', 's_liq_z']:
                            if (now_ts - ut > STALE_CHECK_TIMEOUT) or abs(data[k]) < 1e-6:
                                data[k] = i_m.get(_STALE_FALLBACK_KEY.get(k, k), 0.0)
                        
                        # [P2-1] Lightweight matrix update replaces full calculate_hybrid_score(rank_maps=None).
                        # Fix: old code double-mutated persistence_history (hist state updated in both Pass 1
                        # and Pass 2 with same data → rsi_delta/vwap_vel zeroed in Pass 2).
                        is_active = sym in intel.active_position_symbols
                        logic.update_matrices_only(sym, data)

                        # [V61.5 FIX] Unify absorption_power with Dual-Zone LOB metric (front_rep_z)
                        # This ensures consistency between sovereign and core_logic.
                        absorption_power = max(0.0, data['front_rep_z'])

                        # [P2-2] In-place update: avoids new top-level dict allocation + GC pressure each tick
                        self.market_data_cache[sym] = {
                            'cp': p, 'vwap_z': vz, 'rsi5': r5_calib, 'rsi15': r15_calib,
                            'abs_z': data['abs_z'], 'acc_z': data['acc_z'], 'eff_z': ez,
                            'oi_z': data['oi_z'], 'cvd_z': data['cvd_z'],
                            'absorption_power': absorption_power, 'fric': fric,
                            'cvd_vel': cv, 'l_liq_z': data['l_liq_z'], 's_liq_z': data['s_liq_z'],
                            'score_short': 0.0, 'score_long': 0.0,  # updated in-place after Pass 2
                            'th': logic.dynamic_threshold * logic.regime_multipliers['score_th_mult']
                        }
                        seen_syms.add(sym)
                        scan_queue.append((sym, data, is_active, m))  # carry m ref for Pass 2 SHM write

                        try:
                            m.oi_z, m.oi_raw, m.rsi5, m.rsi15 = data['oi_z'], data['oi_raw'], r5_calib, r15_calib
                            m.score_short = m.score_long = 0.0  # final scores written in Pass 2 via m_ref
                        except Exception: pass

                    except Exception as sym_e:
                        if "struct" not in str(sym_e): logging.error(f"⚠️ Scanner Error ({i}): {sym_e}")
                        continue

                # RE-RANK & UPDATE CONTEXT (Sync Point)
                # [P2-2] Prune symbols not seen this tick (removed from SHM since last scan)
                for _s in [k for k in self.market_data_cache if k not in seen_syms]:
                    del self.market_data_cache[_s]
                logic.update_market_context(self.market_data_cache)
                intel.update_oi_snapshot(self.market_data_cache)

                # [P2-3] heapq.nlargest: O(n log k) vs sorted O(n log n) for top-k rank extraction
                rank_maps = {
                    'l_rank':  {s: r+1 for r, (s, _) in enumerate(heapq.nlargest(RANK_LIMIT_NFE_POOL, logic.nfe_matrix_long.items(),  key=lambda x: x[1]))},
                    's_rank':  {s: r+1 for r, (s, _) in enumerate(heapq.nlargest(RANK_LIMIT_NFE_POOL, logic.nfe_matrix_short.items(), key=lambda x: x[1]))},
                    'oi_rank': {s: r+1 for r, (s, _) in enumerate(heapq.nlargest(RANK_LIMIT_OI_POOL,  logic.oi_matrix.items(),         key=lambda x: x[1]))}
                }

                # PASS 2: FINAL DECISIONS WITH FRESH RANKINGS
                for sym, data, is_active, m_ref in scan_queue:
                    score, sig, mode, side, det = logic.calculate_hybrid_score(sym, data, intel, rank_maps, is_active_position=is_active)
                    orig_score = det.get('s_score', score)

                    # [P2-2] Update cache with final scores in-place
                    if sym in self.market_data_cache:
                        _c = self.market_data_cache[sym]
                        _c['score_short'] = score if side == 'SHORT' else 0.0
                        _c['score_long']  = score if side == 'LONG'  else 0.0
                        _c['th'] = det.get('th', 350.0)
                    # [P2-1] Write final scores to SHM via m_ref (replaces stale preliminary Pass 1 write)
                    try:
                        _raw = float(det.get('s_score', score))
                        if side == 'SHORT':  m_ref.score_short, m_ref.score_long = _raw, 0.0
                        elif side == 'LONG': m_ref.score_long,  m_ref.score_short = _raw, 0.0
                        else:                m_ref.score_short = m_ref.score_long = 0.0
                    except Exception: pass

                    if sig == "SNIPER":
                        if sym not in intel.rsi_state:
                            await intel.ensure_rsi_ready(self.session, sym)
                            r5_c, r15_c = intel.get_calibrated_rsi(sym, data['cp']), intel.get_calibrated_rsi15(sym, data['cp'])
                            if r5_c > 0: det['rsi5'], det['rsi15'] = r5_c, r15_c
                        results.append((sym, score, side, mode, data['cp'], det))
                    elif orig_score > det.get('th', 350.0) * 0.8:
                        reason = sig if sig != "WAIT" else "GATE_REJECT"
                        if reason == "GATE_REJECT": stats['gate_rejections'] += 1
                        else: stats['veto_rejections'] += 1
                        if orig_score > det.get('th', 350.0):
                            logging.debug(f"🔍 [REJECT] {sym} {side} Sc:{orig_score:.1f} R:{reason} | RSI:{data['rsi5']:.1f}/{data['rsi15']:.1f} VWAP:{data['vwap_z']:+.2f}")

                if shm.symbol_count > 0 and stale_engine_count >= shm.symbol_count:
                    logging.critical(f"💀 ENGINE FREEZE DETECTED. SUICIDE.")
                    sys.exit(1)

                if now_ts - self.last_logic_gc > LOGIC_GC_INTERVAL:
                    active_syms = set(self.market_data_cache.keys()) | self.trader.positions.keys()
                    logic.run_memory_gc(active_syms)
                    self.last_logic_gc = now_ts

                if results:
                    results.sort(key=lambda x: x[1], reverse=True)
                    for sym, score, side, mode, p, det in results[:3]: 
                        asyncio.create_task(self.trader.open_position(sym, side, score, 1, mode, p, intel, det, self.market_data_cache))
                
                await asyncio.sleep(max(0.001, SCANNER_SLEEP_TICK - (time.perf_counter() - start)))

            except Exception as loop_e:
                # [P0-5a FIX] Removed duplicate except block (dead code — second
                # handler was never reachable and shadowed the first).
                logging.critical(f"💥 CRITICAL SCANNER LOOP ERROR: {loop_e}")
                await asyncio.sleep(1.0) # Prevent CPU spin on persistent error

if __name__ == "__main__":
    from logging.handlers import RotatingFileHandler
    
    log_file = os.path.join(BASE_DIR, "logs/sovereign_v43.log")
    file_handler = RotatingFileHandler(log_file, maxBytes=1024*1024, backupCount=1)
    stream_handler = logging.StreamHandler(sys.stdout)
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [SOVEREIGN] %(message)s',
        handlers=[file_handler, stream_handler],
        force=True
    )
    
    try:
        from core_constants import CONFIG
        core_logic.StrategyLogic.initialize(CONFIG)
        engine = SovereignEngine()
        logging.info("🚀 Sovereign System V58.0 RE-CONNECTED TO LOG FILE.")
        asyncio.run(engine.start())
    except Exception as e:
        logging.critical(f"💥 CRITICAL BOOT FAILURE: {e}")
        logging.error(traceback.format_exc())
