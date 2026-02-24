import json
import time
import asyncio
import os
import sqlite3
import logging
import math
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from core_constants import *

class SharkTrader:
    def __init__(self, config: dict):
        self.sys_cfg = config.get('system', {})
        self.tg_cfg = config.get('telegram', {})
        self.db_path = os.path.join(BASE_DIR, "logs/shark_vault.db")
        self.fee_rate = FEE_RATE_TOTAL
        self.margin_ratio = 0.1
        self.global_pnl_stop_limit = GLOBAL_PNL_STOP_LIMIT
        self.lock = asyncio.Lock()
        self.positions = {}
        self.wallet = {"balance": 10000.0}
        self.session = None
        self.loss_streak = {} 
        self.db_queue = asyncio.Queue()
        self._init_db()
        self._load_state_from_db()
        self.db_worker_task = asyncio.create_task(self._db_writer_worker())

    def stop(self):
        if hasattr(self, 'db_worker_task') and self.db_worker_task:
            self.db_worker_task.cancel()

    async def _db_writer_worker(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            while True:
                future = None
                try:
                    task = await self.db_queue.get()
                    if len(task) == 3:
                        func, args, future = task
                    else:
                        func, args = task

                    func(conn, *args); conn.commit()
                    if future and not future.done(): future.set_result(True)
                    self.db_queue.task_done()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logging.error(f"❌ [DB_WORKER] Write Failed: {e}")
                    if future and not future.done(): future.set_exception(e)
                    self.db_queue.task_done()
                    await asyncio.sleep(1)
        finally:
            conn.close()

    async def _execute_db_task(self, func, *args):
        await self.db_queue.put((func, args))

    async def _execute_db_task_with_confirm(self, func, done_event, *args):
        await self.db_queue.put((func, args, done_event))

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS trade_history (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT, symbol TEXT, side TEXT, entry REAL, exit REAL, pnl_pct REAL, pnl_usd REAL, reason TEXT, duration REAL, details TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS wallet (id INTEGER PRIMARY KEY, balance REAL, last_update TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS active_positions (symbol TEXT PRIMARY KEY, entry REAL, type TEXT, qty REAL, entry_margin REAL, max_pnl REAL, start_time REAL, details TEXT)")
            # [V58.0 Patch] Add 'lev' column if missing
            try:
                conn.execute("ALTER TABLE active_positions ADD COLUMN lev INTEGER DEFAULT 10")
            except sqlite3.OperationalError:
                pass 
            conn.execute("CREATE TABLE IF NOT EXISTS circuit_breaker (symbol TEXT PRIMARY KEY, count INTEGER, last_time REAL)")
            if not conn.execute("SELECT id FROM wallet WHERE id=1").fetchone():
                conn.execute("INSERT INTO wallet (id, balance, last_update) VALUES (1, 10000.0, datetime('now'))")

    def _load_state_from_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            w = conn.execute("SELECT balance FROM wallet WHERE id=1").fetchone()
            if w: self.wallet['balance'] = w['balance']
            rows = conn.execute("SELECT * FROM active_positions").fetchall()
            self.positions = {r['symbol']: {
                'entry': r['entry'], 'type': r['type'], 'qty': r['qty'], 
                'entry_margin': r['entry_margin'], 'start_time': r['start_time'], 
                'lev': r['lev'] if 'lev' in r.keys() else DEFAULT_LEVERAGE,
                'details': json.loads(r['details'])
            } for r in rows}
            cb_rows = conn.execute("SELECT * FROM circuit_breaker").fetchall()
            self.loss_streak = {r['symbol']: {'count': r['count'], 'last_time': r['last_time']} for r in cb_rows}

    async def update_pnl(self, market_data: dict, intel, current_time: float) -> Tuple[None, None, List]:
        exit_list = []
        updates = []
        async with self.lock:
            pos_snapshot = list(self.positions.items())
        for i, (sym, pos) in enumerate(pos_snapshot):
            if i > 0 and i % 10 == 0: await asyncio.sleep(0)
            if pos.get('is_closing'): continue
            if sym not in market_data: continue
            curr_p = market_data[sym].get('cp', 0)
            if curr_p <= 0: continue

            # --- IRONSHIELD: FLASH CRASH PROTECTION (V60.0) ---
            # Prevent false stop-loss on bad ticks or symbol swaps (e.g. 0.65 -> 0.04)
            # If price drops > 40% instantly, ignore it.
            price_drop_pct = (pos['entry'] - curr_p) / pos['entry']
            price_rise_pct = (curr_p - pos['entry']) / pos['entry']
            
            # [V61.2 FIX] IronShield Paradox: Evaluate Hard Stop even on flash anomalies
            net_pct = self._calculate_net_pnl(pos, curr_p) # Moved up to prevent NameError
            if pos['type'] == 'LONG' and price_drop_pct > 0.40:
                logging.warning(f"🛡️ [IronShield] Flash Crash Detected (LONG): {sym}")
                if net_pct < -0.6: exit_list.append((sym, curr_p, "Hard Stop (IronShield)")); continue
                continue
            if pos['type'] == 'SHORT' and price_rise_pct > 0.40:
                logging.warning(f"🛡️ [IronShield] Flash Pump Detected (SHORT): {sym}")
                if net_pct < -0.6: exit_list.append((sym, curr_p, "Hard Stop (IronShield)")); continue
                continue
            
            duration = current_time - pos.get('start_time', current_time)
            is_new_peak = False
            max_p = pos['details'].get('max_price', pos['entry'])
            if (pos['type'] == 'LONG' and curr_p > max_p) or (pos['type'] == 'SHORT' and curr_p < max_p):
                pos['details']['max_price'] = curr_p; is_new_peak = True
            reason = self._evaluate_exit_conditions(sym, pos, market_data[sym], net_pct, duration)
            if reason: exit_list.append((sym, curr_p, reason))
            elif is_new_peak: updates.append((sym, pos['details']))
        for sym, det in updates: await self._execute_db_task(self._db_update_details, sym, json.dumps(det))
        return None, None, exit_list

    def _db_update_details(self, conn, sym, det_json):
        conn.execute("UPDATE active_positions SET details=? WHERE symbol=?", (det_json, sym))

    def _evaluate_exit_conditions(self, sym, pos, d, net_pct, duration) -> Optional[str]:
        entry_p = pos['entry']; curr_p = d['cp']; max_p = pos['details'].get('max_price', entry_p)
        peak_roe = (abs(max_p - entry_p) / entry_p * DEFAULT_LEVERAGE) - (FEE_RATE_TOTAL * DEFAULT_LEVERAGE * 2.0)
        mode = pos['details'].get('mode', 'NONE')
        
        # 1. HARD STOP (Absolute Safety Net - Relaxed for Swing)
        # -5% Gross Loss (~ -5.5% Net PnL) is the final line in the sand.
        if net_pct <= HARD_STOP_LOSS_NET: return f"Hard Stop-Loss Triggered ({net_pct*100:+.2f}%)"

        # 2. ORPHAN CLEANUP (Mode 'NONE' Defense)
        # If mode is lost (DB restore artifact), exit after 5 mins to prevent zombie positions
        if mode == "NONE" and duration > GRACE_PERIOD_SEC:
            return "Orphan Position Cleanup (Mode NONE)"

        # 3. REVERSE SCORE EXIT (DISABLED V58.0)
        # Reason: Score infrastructure currently only returns dominant side score.
        # opp_score = d.get('score_short', 0.0) if pos['type'] == 'LONG' else d.get('score_long', 0.0)
        # if opp_score > (d.get('th', 350.0) * 0.7): 
        #      return f"Reverse Signal Exit (Score: {opp_score:.0f})"

        # 4. MODE-SPECIFIC LOGIC
        if mode == "APEX-REVERSAL":
            # A. Price Target (VWAP Drift-Free)
            # Target = Entry * (1 +/- (Entry_VWAP_Z * 0.004))
            vwap_z_entry = abs(pos['details'].get('vwap', 0.0))
            target_roi = vwap_z_entry * APEX_TARGET_ROI_MULT # e.g. Z=3.0 -> 1.2% Target ROI
            
            # Directional ROI Check
            raw_roi = (curr_p - entry_p) / entry_p if pos['type'] == 'LONG' else (entry_p - curr_p) / entry_p
            
            if raw_roi >= target_roi and net_pct > 0.005:
                return f"APEX Target Hit (ROI: {raw_roi*100:.2f}%)"
            
            # B. Absorption Decay (Reason for entry vanished)
            # If current absorption < 30% of entry absorption, exit
            entry_abs = pos['details'].get('absorption', 0.0)
            curr_abs = d.get('absorption_power', 0.0) 
            
            if entry_abs > 50.0 and curr_abs < (entry_abs * APEX_ABSORPTION_DECAY_RATE) and duration > 60:
                return f"Absorption Wall Collapsed ({curr_abs:.1f} < {entry_abs*APEX_ABSORPTION_DECAY_RATE:.1f})"

        elif mode == "WHALE-FORCE":
            # A. Liquidation Exhaustion (Fuel Monitoring)
            # LONG is fueled by SHORT liquidations (s_liq_z), SHORT is fueled by LONG liquidations (l_liq_z)
            fuel_z = d.get('s_liq_z', 0.0) if pos['type'] == 'LONG' else d.get('l_liq_z', 0.0)
            
            # If opposite side liquidations (fuel) drop below threshold, the whale-driven move is exhausting
            if fuel_z < WHALE_LIQ_EXHAUST_Z and duration > 30:
                return f"Whale Fuel Exhausted (Opp-Liq-Z: {fuel_z:.2f})"
            
            # B. CVD Momentum Reversal (Real-time WS)
            cvd_vel = d.get('cvd_vel', 0.0)
            # Increased threshold to 0.15 to avoid noise
            if (pos['type'] == 'LONG' and cvd_vel < -WHALE_MOMENTUM_REVERSAL_VEL) or \
               (pos['type'] == 'SHORT' and cvd_vel > WHALE_MOMENTUM_REVERSAL_VEL):
                if duration > 45: return f"Momentum Reversal (CVD Vel: {cvd_vel:.3f})"

        # 4. TRAILING STOP (Swing Mode: Let Profits Run)
        trail_dist = TRAIL_DIST_DEFAULT 
        
        if mode == "APEX-REVERSAL":
            # Reversal trades need tighter initial protection, then loosen up
            if net_pct > 0.02: trail_dist = TRAIL_APEX_TIGHT # Secure 1% if up 2%
            elif net_pct > 0.01: trail_dist = TRAIL_APEX_MID # Breakeven+
        
        elif mode == "WHALE-FORCE":
            # Trend following needs space early on
            if net_pct > 0.05: # Massive run (>5%)
                trail_dist = TRAIL_WHALE_V_HIGH # Lock it tight (0.5%)
            elif net_pct > 0.03: # Strong run (>3%)
                trail_dist = TRAIL_WHALE_HIGH # Secure major chunk (1.0%)
            elif net_pct > 0.015: # Early trend (>1.5%)
                trail_dist = TRAIL_WHALE_MID # Move to Breakeven
            else:
                trail_dist = TRAIL_WHALE_INITIAL # Give 2.5% room for initial volatility

        # Execute Trail
        p_dist = (max_p - curr_p) / max_p if pos['type'] == 'LONG' else (curr_p - max_p) / max_p
        if p_dist > trail_dist:
             return f"Trailing Stop (Dist: {p_dist*100:.2f}%, Limit: {trail_dist*100:.2f}%)"

        return None

    def _format_qty(self, symbol, qty, intel):
        f = intel.symbol_filters.get(symbol, {}); step = f.get('stepSize', 0.0)
        return float(math.floor(qty / step) * step) if step > 0 else round(qty, 1)

    async def open_position(self, symbol, side, score, rank, mode, price, intel, details, market_data=None):
        async with self.lock:
            if not self._can_open(symbol): return False

            # --- IRONSHIELD: REAL GLOBAL PNL INTEGRITY CHECK (V58.0 Patch) ---
            current_total_pnl = 0.0
            if market_data:
                for s, p in self.positions.items():
                    curr_p = market_data.get(s, {}).get('cp', p['entry']) # Fallback to entry if not in cache
                    current_total_pnl += self._calculate_net_pnl(p, curr_p)

            if self.wallet['balance'] * (1 + current_total_pnl) < self.wallet['balance'] * (1 + self.global_pnl_stop_limit):
                logging.warning(f"⚠️ [IronShield] Entry Blocked: Global PnL Limit Hit ({current_total_pnl:.2%})")
                return False

            conf = SIZING_HIGH_MULT if score > SIZING_HIGH_CONF_SCORE else (SIZING_LOW_MULT if score < SIZING_LOW_CONF_SCORE else 1.0)
            margin = self.wallet['balance'] * self.margin_ratio * conf
            qty = self._format_qty(symbol, (margin * DEFAULT_LEVERAGE) / price, intel)
            if qty <= 0: return False
            pos = {'entry': price, 'type': side, 'qty': qty, 'entry_margin': margin, 'lev': DEFAULT_LEVERAGE, 'start_time': time.time(), 'details': {**details, "max_price": price}}
            self.positions[symbol] = pos

            # [V61.5 FIX] DB task must be queued INSIDE lock to prevent race with close_position
            # We don't await the confirmation here to keep entry fast, but order is now guaranteed.
            asyncio.create_task(self._execute_db_task(self._db_add_pos, symbol, price, side, qty, margin, DEFAULT_LEVERAGE, pos['details']))

            self._notify_open(symbol, side, price, score, mode, details)
            return True
    def _db_add_pos(self, conn, sym, p, s, q, m, lev, det):
        conn.execute("INSERT OR REPLACE INTO active_positions VALUES (?,?,?,?,?,?,?,?,?)", (sym, p, s, q, m, 0.0, time.time(), json.dumps(det), lev))

    async def close_position(self, symbol, exit_price, reason, snapshot=None):
        # 1. PRE-CHECK & LOCKING
        async with self.lock:
            if symbol not in self.positions: return
            pos = self.positions[symbol]
            if pos.get('is_closing'): return 
            pos['is_closing'] = True
            
            net_pnl = self._calculate_net_pnl(pos, exit_price)
            pnl_usd = pos['entry_margin'] * net_pnl
            
            # --- IRONSHIELD: ANOMALY PNL PROTECTION ---
            if abs(pnl_usd) > 5000.0:
                logging.critical(f"🚨 [IronShield] ANOMALY DETECTED: {symbol} PnL ${pnl_usd:.2f}!")

            # [V60.3 FIX] Transactional Integrity: DB First, Memory Second
            future = asyncio.Future()
            # Pass pnl_usd instead of calculated new_balance to ensure atomic update
            await self._execute_db_task_with_confirm(self._db_close_pos, future, symbol, exit_price, net_pnl, pnl_usd, reason, pos)
            
        try:
            # 2. DB WAIT (LOCK RELEASED)
            await asyncio.wait_for(future, timeout=5.0)
            
            # 3. FINALIZATION (RE-LOCK)
            async with self.lock:
                if symbol not in self.positions: return
                
                # SUCCESS: Apply atomic increment to balance
                self.wallet['balance'] += pnl_usd
                
                if net_pnl < LOSS_STREAK_THRESHOLD: 
                    self.loss_streak[symbol] = {'count': self.loss_streak.get(symbol, {}).get('count', 0) + 1, 'last_time': time.time()}
                else: 
                    self.loss_streak[symbol] = {'count': 0, 'last_time': 0}
                
                self._notify_close(symbol, pos, exit_price, net_pnl, pnl_usd, reason)
                del self.positions[symbol]
                
        except Exception as e:
            logging.error(f"❌ [DB] Close failed for {symbol}: {e}. State retained.")
            async with self.lock:
                if symbol in self.positions: self.positions[symbol]['is_closing'] = False

    def _db_close_pos(self, conn, sym, ex_p, n_p, p_usd, reas, pos):
        conn.execute("DELETE FROM active_positions WHERE symbol=?", (sym,))
        # Use atomic increment in SQL to prevent race conditions
        conn.execute("UPDATE wallet SET balance = balance + ?, last_update=datetime('now') WHERE id=1", (p_usd,))
        conn.execute("INSERT INTO trade_history (time, symbol, side, entry, exit, pnl_pct, pnl_usd, reason, duration, details) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), sym, pos['type'], pos['entry'], ex_p, round(n_p*100, 2), round(p_usd, 2), reas, round(time.time()-pos['start_time'], 1), json.dumps(pos['details'])))

    def _calculate_net_pnl(self, pos, curr_p) -> float:
        if curr_p <= 0: return 0.0
        raw = (curr_p - pos['entry']) / pos['entry'] if pos['type'] == 'LONG' else (pos['entry'] - curr_p) / pos['entry']
        return (raw * pos.get('lev', 10)) - (FEE_RATE_TOTAL * pos.get('lev', 10) * 2.0)

    def _can_open(self, symbol) -> bool:
        if symbol in self.positions or self.wallet['balance'] <= 0 or len(self.positions) >= self.sys_cfg.get('max_positions', 5): return False
        streak = self.loss_streak.get(symbol, {})
        if streak.get('count', 0) >= 3 and (time.time() - streak.get('last_time', 0)) < 300: return False
        return True

    def _notify_open(self, symbol, side, price, score, mode, details):
        if not self.session or not self.tg_cfg.get('enabled'): return
        emoji = '🟢 *LONG*' if side == 'LONG' else '🔴 *SHORT*'
        vwap_z = details.get('vwap', 0.0); rsi5 = details.get('rsi5', 0.0); rsi15 = details.get('rsi15', 0.0)
        oi_z = details.get('oi_z', 0.0); cvd_z = details.get('cvd_z', 0.0); dtd = details.get('dtd', 0.0)
        fric = details.get('fric', 0.0); syn = details.get('syn', 0)
        abs_pwr = details.get('absorption', 0.0); squeeze = details.get('squeeze', 0.0)
        ts_str = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        
        header = "🚨 *[APEX SIREN]*" if abs(vwap_z) > 3.5 else f"🚀 *[SHARK V58.0]*"
        msg = (f"{header} *NEW ENTRY* `[{ts_str}]`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🎯 *#{symbol} [{emoji}]*\n"
               f"🏷️ `Mode: {mode}`\n"
               f"🔥 `Synergy: {syn}` | 📊 `Score: {score:.1f}`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🌐 `VWAP-Z: {vwap_z:+.2f}`\n"
               f"🔋 `OI-Z: {oi_z:+.2f}` | 📉 `CVD-Z: {cvd_z:+.2f}`\n"
               f"🛡️ `Absorb: {abs_pwr:.1f}` | 🍋 `Sqze: {squeeze:.1f}`\n"
               f"⚡ `DTD Eff: {dtd:.2f}` | 🧱 `Fric: {fric:.2f}`\n"
               f"📈 `RSI: {rsi5:.1f} (5m) / {rsi15:.1f} (15m)`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"💵 `Entry: {price:.5f}`\n"
               f"💰 `Wallet: ${self.wallet['balance']:,.2f}`")
        asyncio.create_task(self.send_telegram(msg))

    def _notify_close(self, symbol, pos, exit_p, net_p, pnl_usd, reason):
        if not self.session or not self.tg_cfg.get('enabled'): return
        emoji = '🟢 LONG' if pos['type'] == 'LONG' else '🔴 SHORT'
        duration = time.time() - pos.get('start_time', time.time())
        m, s = divmod(int(duration), 60)
        entry_p = pos['entry']; max_p = pos['details'].get('max_price', entry_p)
        peak_roe = (abs(max_p - entry_p) / entry_p * DEFAULT_LEVERAGE) - (FEE_RATE_TOTAL * DEFAULT_LEVERAGE * 2.0)
        ts_str = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        
        msg = (f"🏁 *SHARK EXIT REPORT* `[{ts_str}]`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"🎯 *#{symbol} [{emoji}]*\n"
               f"🚪 `Reason: {reason}`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"💰 *PnL: {net_p*100:+.2f}%* (`${pnl_usd:+.2f}`)\n"
               f"🔝 *Peak ROE: {peak_roe*100:+.2f}%*\n"
               f"⏱ `Duration: {m}m {s}s`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"💵 `Entry: {entry_p:.5f}`\n"
               f"💵 `Exit:  {exit_p:.5f}`\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"💰 `Final Bal: ${self.wallet['balance']:,.2f}`")
        asyncio.create_task(self.send_telegram(msg))

    async def send_telegram(self, msg):
        if not self.session or not self.tg_cfg.get('enabled'): return
        url = f"https://api.telegram.org/bot{self.tg_cfg.get('token')}/sendMessage"
        try: await self.session.post(url, json={'chat_id': self.tg_cfg.get('chat_id'), 'text': msg, 'parse_mode': 'Markdown'})
        except Exception as e: logging.debug(f"TG Send Failed: {e}")

    def set_session(self, session): self.session = session
