import asyncio
import aiohttp
import time
import json
import logging
import os
import random
from collections import deque
from core_constants import *

class MarketIntelligence:
    def __init__(self):
        self.avg_vol_5m = {}
        self.price_24h_change = {}
        self.symbols = []
        self.symbol_filters = {} 
        self.struct_low_15m = {}
        self.struct_high_15m = {}
        self.oi_data = {}
        self.oi_history = {}
        self.oi_sum = {}
        self.oi_sum_sq = {}
        self.oi_z_cache = {}
        self.oi_timestamps = {}
        self.cvd_history = {}
        self.cvd_sum = {}
        self.cvd_sum_sq = {}
        self.cvd_z_cache = {}
        self.liq_history = {'LONG': {}, 'SHORT': {}}
        self.liq_sum = {'LONG': {}, 'SHORT': {}}
        self.liq_sum_sq = {'LONG': {}, 'SHORT': {}}
        self.liq_z_cache = {'LONG': {}, 'SHORT': {}}
        self.trading_status = {}
        self.active_position_symbols = set()
        self.data_timestamps = {}
        self.last_gc_time = time.time()
        self.last_status_check = 0
        self.last_ticker_check = 0
        self.last_struct_update = 0
        self.last_symbol_update = 0
        self.ws_connected = False
        self.symbol_backoffs = {}
        self.oi_last_fetch = {}
        self.api_key = None
        
        self.rsi_history = {} 
        self.rsi15_history = {}
        self.rsi_state = {} 
        self.rsi15_state = {}
        self.last_rsi_sync = 0
        # --- PHASE 2: CONCURRENCY CONTROL ---
        self._oi_semaphore = None

    @property
    def oi_semaphore(self):
        if self._oi_semaphore is None:
            self._oi_semaphore = asyncio.Semaphore(5)
        return self._oi_semaphore

    def _calculate_wilder_rsi(self, prices, period=14):
        if len(prices) < period + 1: return 50.0, 50.0
        deltas = [prices[i+1] - prices[i] for i in range(len(prices)-1)]
        up_moves = [d if d > 0 else 0 for d in deltas]
        down_moves = [-d if d < 0 else 0 for d in deltas]
        
        # Initial Simple Average
        avg_up = sum(up_moves[:period]) / period
        avg_down = sum(down_moves[:period]) / period
        
        # Wilder's Smoothing
        for i in range(period, len(up_moves)):
            avg_up = (avg_up * (period - 1) + up_moves[i]) / period
            avg_down = (avg_down * (period - 1) + down_moves[i]) / period
        
        if avg_down == 0:
            # Return averages, not RSI values. Use small epsilon for safety.
            return (avg_up if avg_up > 0 else 0.01), 0.000001
            
        return avg_up, avg_down

    def _get_rsi_from_state(self, avg_up, avg_down):
        if avg_down == 0:
            return 100.0 if avg_up > 0 else 50.0
        rs = avg_up / avg_down
        return 100.0 - (100.0 / (1.0 + rs))

    async def _sync_all_rsi_klines(self, session):
        targets = [s for s in self.symbols if self.trading_status.get(s) == 'TRADING']
        for i in range(0, len(targets), 15):
            batch = targets[i:i+15]
            await asyncio.gather(*[self._fetch_rsi_kline(session, s) for s in batch])
            await asyncio.sleep(0.2)
        self.last_rsi_sync = time.time()

    async def _fetch_rsi_kline(self, session, symbol):
        headers = {'X-MBX-APIKEY': self.api_key} if self.api_key else {}
        try:
            url5 = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=5m&limit=500"
            async with session.get(url5, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices = [float(k[4]) for k in data]
                    self.rsi_history[symbol] = {'last_close': prices[-2], 'last_ts': int(data[-1][0]) / 1000}
                    au, ad = self._calculate_wilder_rsi(prices[:-1])
                    self.rsi_state[symbol] = {'au': au, 'ad': ad, 'lp': prices[-2]}
            
            url15 = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=15m&limit=500"
            async with session.get(url15, headers=headers, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prices = [float(k[4]) for k in data]
                    self.rsi15_history[symbol] = {'last_close': prices[-2], 'last_ts': int(data[-1][0]) / 1000}
                    au15, ad15 = self._calculate_wilder_rsi(prices[:-1])
                    self.rsi15_state[symbol] = {'au': au15, 'ad': ad15, 'lp': prices[-2]}
        except Exception as e:
            logging.warning(f"RSI Kline Fetch Failed [{symbol}]: {e}")

    async def ensure_rsi_ready(self, session, symbol):
        """[V58.0 Patch] Ensure RSI is ready for new symbols."""
        if symbol not in self.rsi_state:
            await self._fetch_rsi_kline(session, symbol)

    def get_calibrated_rsi(self, symbol, current_price):
        data = self.rsi_history.get(symbol); state = self.rsi_state.get(symbol)
        if not data or not state: return 0.0
        diff = current_price - state['lp']
        up = diff if diff > 0 else 0; down = -diff if diff < 0 else 0
        curr_au = (state['au'] * 13 + up) / 14; curr_ad = (state['ad'] * 13 + down) / 14
        return self._get_rsi_from_state(curr_au, curr_ad)

    def get_calibrated_rsi15(self, symbol, current_price):
        data = self.rsi15_history.get(symbol); state = self.rsi15_state.get(symbol)
        if not data or not state: return 0.0
        diff = current_price - state['lp']
        up = diff if diff > 0 else 0; down = -diff if diff < 0 else 0
        curr_au = (state['au'] * 13 + up) / 14; curr_ad = (state['ad'] * 13 + down) / 14
        return self._get_rsi_from_state(curr_au, curr_ad)

    def _update_cvd_stats(self, sym, val):
        # [P1-2] O(1) rolling sum / sum-of-squares — mirrors _update_oi_stats.
        # The previous implementation used Welford's algorithm for the warmup
        # phase but fell back to an O(2n) full list recompute once the window
        # reached capacity (100 samples), which was called on every trade tick.
        # cvd_sum and cvd_sum_sq are already declared in __init__ and cleaned
        # by _run_gc, so no new fields are introduced.
        if sym not in self.cvd_history:
            self.cvd_history[sym]  = deque(maxlen=100)
            self.cvd_sum[sym]      = 0.0
            self.cvd_sum_sq[sym]   = 0.0

        if len(self.cvd_history[sym]) == 100:
            old_val = self.cvd_history[sym][0]
            self.cvd_sum[sym]    -= old_val
            self.cvd_sum_sq[sym] -= old_val * old_val

        self.cvd_history[sym].append(val)
        self.cvd_sum[sym]    += val
        self.cvd_sum_sq[sym] += val * val

        h_len = len(self.cvd_history[sym])
        if h_len >= 10:
            mean = self.cvd_sum[sym] / h_len
            var  = (self.cvd_sum_sq[sym] / h_len) - (mean * mean)
            std  = max(0.0, var) ** 0.5
            self.cvd_z_cache[sym] = (val - mean) / std if std > 0.01 else 0.0
        else:
            self.cvd_z_cache[sym] = 0.0

    def _update_oi_stats(self, sym, val, ts=None):
        if not sym: return
        now = ts if ts else time.time()
        if now <= self.oi_timestamps.get(sym, 0): return
        self.oi_timestamps[sym] = now
        
        if sym not in self.oi_history:
            self.oi_history[sym] = deque(maxlen=240)
            self.oi_sum[sym] = 0.0
            self.oi_sum_sq[sym] = 0.0
            
        # O(1) Rolling Sum and Sum of Squares
        if len(self.oi_history[sym]) == 240:
            old_val = self.oi_history[sym][0]
            self.oi_sum[sym] -= old_val
            self.oi_sum_sq[sym] -= old_val * old_val
            
        self.oi_history[sym].append(val)
        self.oi_sum[sym] += val
        self.oi_sum_sq[sym] += val * val
        self.oi_data[sym] = val
        
        h_len = len(self.oi_history[sym])
        if h_len >= 3:
            # Var = E[X^2] - (E[X])^2
            mean = self.oi_sum[sym] / h_len
            var = (self.oi_sum_sq[sym] / h_len) - (mean * mean)
            std = max(0.0, var)**0.5
            if std > 0.01:
                z = (val - mean) / std
                self.oi_z_cache[sym] = (z * 1.5) * min(1.0, h_len / 20.0)
            else: self.oi_z_cache[sym] = 0.0
        else: self.oi_z_cache[sym] = 0.0

    def _update_liq_stats(self, sym, val, side):
        if sym not in self.liq_history[side]:
            self.liq_history[side][sym] = deque(maxlen=50); self.liq_sum[side][sym] = 0.0; self.liq_sum_sq[side][sym] = 0.0
        if len(self.liq_history[side][sym]) == 50:
            old_val = self.liq_history[side][sym][0]; self.liq_sum[side][sym] -= old_val; self.liq_sum_sq[side][sym] -= old_val * old_val
        self.liq_history[side][sym].append(val); self.liq_sum[side][sym] += val; self.liq_sum_sq[side][sym] += val * val
        h_len = len(self.liq_history[side][sym])
        if h_len >= 5:
            mean = self.liq_sum[side][sym] / h_len; var = (self.liq_sum_sq[side][sym] / h_len) - (mean * mean); std = max(1.0, var**0.5)
            self.liq_z_cache[side][sym] = (val - mean) / std
        else: self.liq_z_cache[side][sym] = 0.0

    def get_latest_metrics(self, sym: str) -> dict:
        return {'oi_z': self.oi_z_cache.get(sym, 0.0), 'cvd_z': self.cvd_z_cache.get(sym, 0.0), 'liq_l': self.liq_z_cache['LONG'].get(sym, 0.0), 'liq_s': self.liq_z_cache['SHORT'].get(sym, 0.0)}

    async def run_intel_loop(self, session):
        logging.info("🧪 Intel Loop Starting...")
        self.heartbeat_path = os.path.join(BASE_DIR, "logs/intel_heartbeat.ts")
        asyncio.create_task(self._run_liq_stream_loop())
        try:
            with open(CONFIG_PATH, 'r') as f:
                cfg = json.load(f); self.api_key = cfg.get('api', {}).get('binance_key')
        except Exception as e: logging.error(f"Config Load Error: {e}")
        
        logging.info("📡 Fetching Exchange Info...")
        await self._update_exchange_info(session)
        logging.info("📡 Updating Bulk Tickers...")
        await self._update_bulk_tickers(session)
        
        logging.info("📡 RSI Calibration starting in background...")
        asyncio.create_task(self._sync_all_rsi_klines(session))
        
        asyncio.create_task(self._run_oi_rest_pump(session))
        self.last_status_check = self.last_ticker_check = time.time()
        logging.info("✅ Intel Initialized. Launching Pulse.")
        
        while True:
            try:
                now = time.time()
                with open(self.heartbeat_path, "w") as f: f.write(str(now))
                if now - self.last_rsi_sync > 300: await self._sync_all_rsi_klines(session)
                if now - self.last_status_check > DISCOVERY_INTERVAL: await self._update_exchange_info(session); self.last_status_check = now
                if now - self.last_ticker_check > 10: await self._update_bulk_tickers(session); self.last_ticker_check = now
                if now - self.last_struct_update > 60:
                    targets = [s for s in (set(self.symbols) | self.active_position_symbols) if self.trading_status.get(s) == 'TRADING']
                    for i in range(0, len(targets), 20):
                        await asyncio.gather(*[self._fetch_struct_only(session, s) for s in targets[i:i+20]])
                        await asyncio.sleep(0.2)
                    self.last_struct_update = now
                if now - self.last_gc_time > DISCOVERY_INTERVAL: self._run_gc()
            except Exception as e:
                logging.error(f"Intel Loop Error: {e}")
                await asyncio.sleep(5)
            await asyncio.sleep(1)

    async def _run_liq_stream_loop(self):
        url = "wss://fstream.binance.com/stream?streams=!forceOrder@arr"
        backoff = 5
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, max_msg_size=0, heartbeat=15) as ws:
                        self.ws_connected = True
                        backoff = 5  # Reset on successful connection
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                payload = json.loads(msg.data)
                                raw = payload.get('data')
                                for item in (raw if isinstance(raw, list) else [raw]):
                                    if not item: continue
                                    o = item.get('o', {}); sym, side = o.get('s'), o.get('S')
                                    val = float(o.get('q', 0)) * float(o.get('p', 0))
                                    s_type = 'SHORT' if side == 'BUY' else 'LONG'
                                    self._update_liq_stats(sym, val, s_type)
            except Exception as e:
                logging.warning(f"Liq WS Disconnected: {e}. Retry in {backoff}s")
                self.ws_connected = False
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)

    async def _run_oi_rest_pump(self, session):
        idx = 0
        while True:
            try:
                now = time.time(); headers = {'X-MBX-APIKEY': self.api_key} if self.api_key else {}
                
                # Priority 1: Active positions (per-symbol 2s cooldown to prevent burst)
                priority = set(self.active_position_symbols)  # Snapshot to avoid RuntimeError during iteration
                for sym in priority:
                    if (self.trading_status.get(sym) == 'TRADING'
                            and now > self.symbol_backoffs.get(sym, 0)
                            and now - self.oi_last_fetch.get(sym, 0) >= 2.0):
                        await self._throttled_oi_fetch(session, sym, headers)
                        self.oi_last_fetch[sym] = now
                
                # Priority 2: Full Rotation (Conservative Stagger)
                rotation_batch = 1
                if self.symbols:
                    for _ in range(rotation_batch):
                        target_sym = self.symbols[idx % len(self.symbols)]
                        if target_sym not in priority and self.trading_status.get(target_sym) == 'TRADING' and now > self.symbol_backoffs.get(target_sym, 0):
                            await self._throttled_oi_fetch(session, target_sym, headers)
                        idx += 1
                
                # [V61.5 Fix] Throttled Rotation: 0.2s -> ~5 req/s (Binance Safe Zone)
                await asyncio.sleep(0.2) 
            except Exception: await asyncio.sleep(2)

    async def _throttled_oi_fetch(self, session, sym, headers):
        # [P1-3] Fire-and-forget: two mechanisms cap throughput:
        #   1. _run_oi_rest_pump sleeps 200ms per outer loop iteration (~5/s cap).
        #   2. oi_semaphore(5) limits concurrent in-flight HTTP requests.
        asyncio.create_task(self._fetch_single_oi(session, sym, headers))

    async def _fetch_single_oi(self, session, sym, headers):
        async with self.oi_semaphore: # --- PHASE 2: LIMIT IN-FLIGHT REQUESTS ---
            try:
                url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={sym}"
                async with session.get(url, headers=headers, timeout=3) as resp:
                    if resp.status == 200:
                        val = float((await resp.json()).get('openInterest', 0))
                        if val > 0: self._update_oi_stats(sym, val)
                        # Clear backoff on success
                        self.symbol_backoffs[sym] = 0
                    elif resp.status == 429:
                        # PENALIZE ONLY THE OFFENDING SYMBOL
                        self.symbol_backoffs[sym] = time.time() + 30.0 + random.uniform(0, 30.0) # [V61.1 FIX] Add Jitter
                        logging.warning(f"⚠️ [INTEL] 429 for {sym}. Penalty: 30s")
            except asyncio.TimeoutError:
                pass # Timeout is normal
            except Exception as e:
                logging.warning(f"OI Fetch Failed [{sym}]: {e}")

    async def _update_exchange_info(self, session):
        try:
            async with session.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.trading_status = {s['symbol']: s['status'] for s in data['symbols']}
                    for s in data['symbols']:
                        f = {fl['filterType']: fl for fl in s['filters']}
                        self.symbol_filters[s['symbol']] = {'stepSize': float(f.get('LOT_SIZE', {}).get('stepSize', 0.001)), 'tickSize': float(f.get('PRICE_FILTER', {}).get('tickSize', 0.001))}
        except Exception as e:
            logging.warning(f"Exchange Info Update Failed: {e}")

    async def _update_bulk_tickers(self, session):
        try:
            async with session.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    # [V60.0 FIX] Dead Symbol Purge: Only process TRADING symbols
                    valid_tickers = [
                        t for t in data 
                        if t['symbol'].endswith('USDT') 
                        and self.trading_status.get(t['symbol']) == 'TRADING'
                    ]
                    
                    for t in valid_tickers:
                        s = t['symbol']
                        self.price_24h_change[s] = float(t['priceChangePercent'])
                        self.avg_vol_5m[s] = float(t['quoteVolume']) / 288.0
        except Exception as e:
            logging.warning(f"Bulk Ticker Update Failed: {e}")

    async def _fetch_struct_only(self, session, symbol):
        try:
            async with session.get(f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=15m&limit=3", timeout=5) as resp:
                if resp.status == 200:
                    res_k = await resp.json(); self.struct_low_15m[symbol] = float(res_k[-2][3]); self.struct_high_15m[symbol] = float(res_k[-2][2]); self.data_timestamps[symbol] = time.time()
        except Exception as e:
            logging.warning(f"Struct Fetch Failed [{symbol}]: {e}")

    def _run_gc(self):
        active = set(self.symbols) | self.active_position_symbols
        # [V61.5 FIX] Enhanced GC for nested dicts and full state integrity (Fixes 4-C)
        for attr in [self.avg_vol_5m, self.price_24h_change, self.struct_low_15m, self.struct_high_15m, self.data_timestamps, self.oi_data, self.oi_history, self.oi_z_cache, self.oi_sum, self.oi_sum_sq, self.oi_timestamps, self.cvd_history, self.cvd_sum, self.cvd_sum_sq, self.cvd_z_cache, self.rsi_history, self.rsi15_history, self.rsi_state, self.rsi15_state, self.oi_last_fetch, self.symbol_backoffs]:
            for k in list(attr.keys()):
                if k not in active:
                    try: del attr[k]
                    except Exception: pass
        
        # Handle nested dicts (Liquidation stats)
        for group in [self.liq_history, self.liq_sum, self.liq_sum_sq, self.liq_z_cache]:
            for side in ['LONG', 'SHORT']:
                for k in list(group[side].keys()):
                    if k not in active:
                        try: del group[side][k]
                        except Exception: pass
                        
        self.last_gc_time = time.time()

    def update_oi_snapshot(self, market_data):
        if not market_data: return
        now = time.time()
        if now - self.last_symbol_update < DISCOVERY_INTERVAL: return
        new_s = set(market_data.keys()); current_s = set(self.symbols)
        if not new_s.issubset(current_s):
            self.symbols = sorted(list(current_s | new_s))[:100]; self.last_symbol_update = now
