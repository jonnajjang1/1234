# Shark Trading Bot - Refactoring Master Plan

## Project Overview
Python async trading bot (Binance Futures) with C++ SHM IPC engine.
Core files: `core_constants.py`, `core_intel.py`, `core_logic.py`, `core_trader.py`, `shark_sovereign_v43.py`, `shark_engine_v37.cpp`

---

## Phase 1: Safety Hardening (No Behavioral Changes)

### 1-1. Hardcoded Path Removal (`core_constants.py`, `shark_sovereign_v43.py`)
- **Problem**: `BASE_DIR = "/home/ninano990707/shark_system"` hardcoded in 2 files
- **Fix**:
  - `core_constants.py:4` — `BASE_DIR = os.environ.get('SHARK_BASE_DIR', os.path.dirname(os.path.abspath(__file__)))`
  - `shark_sovereign_v43.py:3` — Remove duplicate `BASE_DIR`, import from `core_constants`
  - `CONFIG_PATH` derives from `BASE_DIR` already, no change needed

### 1-2. API Key to Environment Variable (`core_intel.py`)
- **Problem**: `core_intel.py:224` reads API key from plaintext JSON config
- **Fix**:
  - `core_intel.py:224` — `self.api_key = os.environ.get('BINANCE_API_KEY') or cfg.get('api', {}).get('binance_key')`
  - Env var takes priority, JSON fallback for backward compat
  - Add `logging.warning` if falling back to JSON key

### 1-3. Telegram Token Protection (`core_trader.py`)
- **Problem**: `core_trader.py:371` — Token in URL string, leaks in logs on error
- **Fix**:
  - Load token via `os.environ.get('SHARK_TG_TOKEN') or self.tg_cfg.get('token')`
  - `core_trader.py:373` — Change debug log to not include URL: `logging.debug(f"TG Send Failed: {type(e).__name__}")`

### 1-4. Input Validation — Symbol Sanitization
- **Problem**: `core_intel.py:329` — Symbol used in URL without encoding
- **Fix**:
  - `core_intel.py:329` — Add `from urllib.parse import quote` and use `quote(sym)`
  - `core_intel.py:98,107,378` — Same treatment for all API URL constructions
  - Add symbol regex guard `re.match(r'^[A-Z0-9]{2,20}USDT$', sym)` before any API call in `_fetch_single_oi`, `_fetch_rsi_kline`, `_fetch_struct_only`

### 1-5. Position Orphan Recovery (`core_trader.py`)
- **Problem**: `core_trader.py:297-299` — If DB write times out, `is_closing=True` stays forever, capital locked
- **Fix**:
  - Already handled in except block (line 299: `pos['is_closing'] = False`)
  - Add periodic orphan scan in `update_pnl()`:
    ```python
    # After pos_snapshot loop, before return
    for sym, pos in pos_snapshot:
        if pos.get('is_closing') and (current_time - pos.get('close_attempt_time', 0)) > 30:
            pos['is_closing'] = False
            logging.warning(f"Recovered orphaned position: {sym}")
    ```
  - Add `close_attempt_time` field when setting `is_closing=True` at line 263

### 1-6. Resource Cleanup — Finally Blocks (`shark_sovereign_v43.py`)
- **Problem**: `shark_sovereign_v43.py:306-310` — SHM cleanup only on CancelledError, not general exceptions
- **Fix**: Move `mm.close()` and `os.close(fd)` into a `finally` block:
  ```python
  try:
      while self.running:
          ...
  except asyncio.CancelledError:
      raise
  except Exception as loop_e:
      ...
  finally:
      mm.close()
      os.close(fd)
  ```

### 1-7. Config Validation (`core_constants.py`)
- **Problem**: No validation on config values. `"DEFAULT_LEVERAGE": "abc"` crashes silently
- **Fix**: Add validation function after `CONFIG = load_full_config()`:
  ```python
  def _validate_config(cfg):
      c = cfg.get('constants', {})
      s = cfg.get('system', {})
      # Type checks
      assert isinstance(s.get('DEFAULT_LEVERAGE', 10), (int, float)), "DEFAULT_LEVERAGE must be numeric"
      # Range checks
      lev = int(s.get('DEFAULT_LEVERAGE', 10))
      assert 1 <= lev <= 125, f"DEFAULT_LEVERAGE {lev} out of range [1, 125]"
      loss = c.get('HARD_STOP_LOSS_NET', -0.055)
      assert -1.0 <= loss <= 0, f"HARD_STOP_LOSS_NET {loss} must be in [-1.0, 0]"
  ```
  - Log warnings for out-of-range values, don't crash (graceful degradation)

---

## Phase 2: Error Handling & Race Condition Fixes

### 2-1. Silent Exception Elimination (`core_intel.py`)
- **Problem**: Multiple `except Exception: logging.warning(...)` that swallow errors silently
- **Fix** (selective — keep non-critical warnings, escalate critical ones):
  - `core_intel.py:115-116` (RSI fetch) — Add `self.rsi_fetch_failures[symbol] = time.time()`, log at ERROR level if 3+ consecutive failures
  - `core_intel.py:340-343` (OI fetch) — Already acceptable (timeout + warning). Add failure counter per symbol
  - Add staleness detection: in `get_calibrated_rsi()`, if `time.time() - data['last_ts'] > 600`, log WARNING once

### 2-2. Race Condition — Lock Timeout (`core_trader.py`)
- **Problem**: `asyncio.Lock()` has no timeout, potential deadlock
- **Fix**:
  - Replace critical lock acquisitions with `asyncio.wait_for(self.lock.acquire(), timeout=10.0)`
  - Wrap in try/except for `asyncio.TimeoutError` — log CRITICAL and skip operation
  - Applies to: `update_pnl` (line 99), `open_position` (line 227), `close_position` (line 259, 282)

### 2-3. DB Worker Crash Recovery (`core_trader.py`)
- **Problem**: If DB worker crashes, all futures hang, positions orphaned
- **Fix**:
  - Add health check in `_db_writer_worker`: if exception count > 5 in 60s, restart worker
  - Add `self.db_worker_healthy = True` flag, checked in `open_position` and `close_position`
  - Auto-restart worker on crash:
    ```python
    async def _ensure_db_worker(self):
        if self.db_worker_task.done():
            logging.error("DB worker died. Restarting...")
            self.db_worker_task = asyncio.create_task(self._db_writer_worker())
    ```

### 2-4. WebSocket Backoff Reset Fix (`core_intel.py`)
- **Problem**: `core_intel.py:282-286` — Backoff not reset if exception during `async for msg` parsing
- **Fix**: Move `backoff = 5` inside the `async with ws_connect` block, after connection established (already done at line 268). The current code is actually correct — backoff resets on successful connection. But add explicit reset after first successful message parse too.

### 2-5. Fire-and-Forget Task Tracking (`core_trader.py`, `core_intel.py`)
- **Problem**: 6+ places where `asyncio.create_task()` is never awaited
- **Fix**:
  - Add `self._background_tasks: set = set()` to both classes
  - Wrapper method:
    ```python
    def _spawn_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(lambda t: self._background_tasks.discard(t))
        if task.done() and task.exception():
            logging.error(f"Background task failed: {task.exception()}")
    ```
  - Replace all `asyncio.create_task(...)` with `self._spawn_task(...)`

### 2-6. DB Parameter Count Bug (`core_trader.py:254-255`)
- **Problem**: `INSERT OR REPLACE INTO active_positions VALUES (?,?,?,?,?,?,?,?,?)` — 9 placeholders
- **Verify**: Table has columns: `symbol, entry, type, qty, entry_margin, max_pnl, start_time, details, lev` = 9 columns
- **Current args**: `(sym, p, s, q, m, 0.0, time.time(), json.dumps(det), lev)` = 9 values
- **Status**: Actually correct (9 = 9). The audit was wrong on this. No fix needed.

---

## Phase 3: Code Quality & Deduplication

### 3-1. Rolling Stats Generic Method (`core_intel.py`)
- **Problem**: `_update_cvd_stats`, `_update_oi_stats`, `_update_liq_stats` — same O(1) rolling sum pattern repeated 3x
- **Fix**: Extract generic method:
  ```python
  def _update_rolling_stats(self, history, sum_dict, sum_sq_dict, key, val, maxlen, min_samples=5):
      if key not in history:
          history[key] = deque(maxlen=maxlen)
          sum_dict[key] = 0.0
          sum_sq_dict[key] = 0.0
      if len(history[key]) == maxlen:
          old = history[key][0]
          sum_dict[key] -= old
          sum_sq_dict[key] -= old * old
      history[key].append(val)
      sum_dict[key] += val
      sum_sq_dict[key] += val * val
      h_len = len(history[key])
      if h_len >= min_samples:
          mean = sum_dict[key] / h_len
          var = (sum_sq_dict[key] / h_len) - (mean * mean)
          std = max(0.0, var) ** 0.5
          return (val - mean) / std if std > 0.01 else 0.0
      return 0.0
  ```
  - Refactor all 3 methods to call this. Keep OI-specific logic (timestamp check, 1.5x multiplier, warmup scaling) as wrapper

### 3-2. RSI Duplicate Removal (`core_intel.py`)
- **Problem**: `get_calibrated_rsi()` and `get_calibrated_rsi15()` are identical except state dict name
- **Fix**: Single method with parameter:
  ```python
  def get_calibrated_rsi(self, symbol, current_price, timeframe='5m'):
      history = self.rsi_history if timeframe == '5m' else self.rsi15_history
      state = self.rsi_state if timeframe == '5m' else self.rsi15_state
      ...
  ```

### 3-3. Cryptic Variable Naming (Targeted)
- Only rename variables that cause genuine confusion, **not cosmetic cleanup**:
  - `m` → keep as `m` (metrics dict, used 100+ times, changing is high-risk noise)
  - `sv()` → `safe_float()` (used in 2 places, low risk)
  - `_c` in `core_constants.py` → `_constants` (module-level, low risk)
  - `dr_p` → `depth_ratio_processed` (only in `_calculate_scores`)
  - `f_s, f_l` → `final_short, final_long` (only in `calculate_hybrid_score`)

### 3-4. Dead Code Removal
- `core_trader.py:153-157` — Commented "REVERSE SCORE EXIT" block. Delete entirely
- `core_logic.py:255` — Stale comment about dict passing. Delete
- `core_logic.py:103` — Comment about removed duplicate. Delete comment only

### 3-5. Magic Number Constants (Critical Only)
- Extract only the most confusing magic numbers into `core_constants.py`:
  ```python
  RSI_NEUTRAL = 50.0
  RSI_PERIOD = 14
  RSI_SMOOTHING = 13  # period - 1
  FLASH_CRASH_THRESHOLD = 0.40  # 40% instant price change = anomaly
  IRONSHIELD_HARD_STOP = -0.6
  ANOMALY_PNL_LIMIT = 5000.0
  ```
- Remaining inline numbers (score formula coefficients) are **strategy parameters** — document with comments, don't extract

### 3-6. Semicolon-Separated Statements
- Split multi-statement lines for readability:
  - `core_intel.py:124` — Split into 2 lines
  - `core_intel.py:128` — Split into 4 lines
  - `core_intel.py:205-208` — Split liq_stats assignments
  - `core_logic.py:265` — Split mode/side/dtd line
  - `shark_sovereign_v43.py:28` — Split __init__ assignments

---

## Phase 4: Architecture (God Class Decomposition)

### 4-1. MarketIntelligence Split (`core_intel.py` → 3 files)
- **`intel/rsi_calculator.py`**: `_calculate_wilder_rsi`, `_get_rsi_from_state`, `get_calibrated_rsi`, `_sync_all_rsi_klines`, `_fetch_rsi_kline`, `ensure_rsi_ready`
- **`intel/oi_fetcher.py`**: `_run_oi_rest_pump`, `_throttled_oi_fetch`, `_fetch_single_oi`, `_update_oi_stats`
- **`intel/liq_tracker.py`**: `_run_liq_stream_loop`, `_update_liq_stats`, `_update_cvd_stats`
- **`core_intel.py`** becomes thin orchestrator importing above 3

### 4-2. StrategyLogic Instance Conversion (`core_logic.py`)
- **Problem**: All static methods + class-level state = hidden global
- **Fix**: Convert to normal instance:
  ```python
  class StrategyLogic:
      def __init__(self, config):
          self.persistence_history = {}
          self.nfe_matrix_long = {}
          ...
  ```
  - `shark_sovereign_v43.py` creates instance: `self.logic = StrategyLogic(CONFIG)`
  - All `StrategyLogic.xxx` calls → `self.logic.xxx`

### 4-3. SovereignEngine Role Separation (`shark_sovereign_v43.py`)
- Currently 4 async loops in 1 class. Keep in same file but clarify responsibilities:
  - `run_scanner_loop` — SHM read + scoring (stays)
  - `run_trader_loop` — PnL + exits (stays)
  - `run_discovery_task` — Symbol rotation (stays)
  - Extract SHM read logic (lines 160-234) into `_read_shm_metrics()` method
  - Extract Pass 2 scoring (lines 255-284) into `_run_scoring_pass()` method

---

## Phase 5: Testing

### 5-1. Unit Tests — Core Math (`tests/test_core_logic.py`)
- RSI calculation: known input → expected output
- `_calculate_scores`: fixed metrics dict → verify score ranges
- `_check_vetoes`: each veto condition individually
- `safe_float` (`sv`): None, NaN, Inf, string inputs
- Regime classification: vol/consensus combinations

### 5-2. Unit Tests — Trader (`tests/test_core_trader.py`)
- `_calculate_net_pnl`: LONG/SHORT with known entry/exit/leverage
- `_can_open`: max positions, loss streak, balance check
- `_evaluate_exit_conditions`: hard stop, trailing stop, APEX target, WHALE fuel exhaustion

### 5-3. Integration Tests (`tests/test_integration.py`)
- Mock SHM data → verify score output
- Mock market data → verify position lifecycle (open → update_pnl → close)
- DB persistence: write position → restart → verify recovery

### 5-4. Test Infrastructure
- `conftest.py` with fixtures: mock intel, mock market_data, mock config
- `pytest.ini` configuration
- No external dependencies (mock Binance API, mock SHM)

---

## Phase 6: Documentation & Strategy Audit (User Collaboration Required)

### 6-1. Magic Constants Documentation
- For each scoring coefficient, add inline comment with:
  - What it controls
  - Reasonable range
  - Whether it was backtested or empirically tuned
- **Requires user input**: Only the author knows derivation history

### 6-2. Trading Logic Review
- RANGE regime gates: verify if tighter/looser is intended
- Synergy multiplier cap: 2.5x may be too aggressive
- Position sizing: no Kelly/volatility scaling — intentional?
- **Requires user input**: Strategy intent clarification

---

## Execution Order
```
Phase 1 (1-2 days)  → Safety, no behavior change, can deploy immediately
Phase 2 (1-2 days)  → Error handling, still no strategy change
Phase 3 (1 day)     → Code quality, cosmetic + dedup
Phase 4 (2-3 days)  → Architecture, highest risk — needs Phase 5 first ideally
Phase 5 (1-2 days)  → Tests for Phase 1-3 changes
Phase 6 (ongoing)   → User collaboration on strategy documentation
```

## Rules
- Never modify scoring coefficients or trading logic behavior without explicit approval
- All changes must be backward compatible with existing `shark_config.json`
- C++ engine (`shark_engine_v37.cpp`) is read-only — do not modify
- SHM struct layout must not change (breaks C++ compatibility)
- Test with `pytest` before any commit
