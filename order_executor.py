"""
Order Executor — Strategy Pattern for Paper / Live trading.

Classes:
    OrderResult    — Immutable fill report returned by every executor method.
    OrderExecutor  — ABC that core_trader.py depends on.
    PaperExecutor  — Wraps current paper-trading behaviour (zero API calls).
    BinanceExecutor — Binance Futures USDT-M real execution.
    RateLimiter    — Token-bucket style rate limiter for Binance.
"""
import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import aiohttp

from binance_signer import BinanceSigner
from core_constants import FAPI_REST_BASE, MAX_CAPITAL


# ---------------------------------------------------------------------------
#  Data
# ---------------------------------------------------------------------------

@dataclass
class OrderResult:
    success: bool
    order_id: Optional[int] = None
    avg_price: float = 0.0
    executed_qty: float = 0.0
    commission: float = 0.0
    status: str = ""
    error_code: Optional[int] = None
    error_msg: str = ""
    raw_response: Optional[dict] = field(default=None, repr=False)


# ---------------------------------------------------------------------------
#  ABC
# ---------------------------------------------------------------------------

class OrderExecutor(ABC):
    """Interface that SharkTrader depends on."""

    @abstractmethod
    async def open_order(self, symbol: str, side: str, qty: float,
                         leverage: int) -> OrderResult:
        """Place an opening MARKET order.  side = 'LONG' | 'SHORT'."""

    @abstractmethod
    async def close_order(self, symbol: str, side: str,
                          qty: float) -> OrderResult:
        """Place a closing MARKET order (reduceOnly)."""

    @abstractmethod
    async def get_balance(self) -> float:
        """Return available USDT balance."""

    @abstractmethod
    async def get_positions(self) -> dict:
        """Return {symbol: {type, qty, entry, leverage, ...}} from exchange."""

    @abstractmethod
    async def emergency_close_all(self) -> list:
        """Kill-switch: market-close every open position."""

    @abstractmethod
    async def initialize(self):
        """Startup validation (position mode, margin type, ping)."""


# ---------------------------------------------------------------------------
#  Paper Executor (current behaviour — no API calls)
# ---------------------------------------------------------------------------

class PaperExecutor(OrderExecutor):
    """100 % backwards-compatible with the old in-memory simulation."""

    def __init__(self, wallet: dict):
        self.wallet = wallet

    async def open_order(self, symbol, side, qty, leverage) -> OrderResult:
        return OrderResult(success=True, avg_price=0.0,
                           executed_qty=qty, status="FILLED")

    async def close_order(self, symbol, side, qty) -> OrderResult:
        return OrderResult(success=True, avg_price=0.0,
                           executed_qty=qty, status="FILLED")

    async def get_balance(self) -> float:
        return self.wallet.get('balance', 0.0)

    async def get_positions(self) -> dict:
        return {}

    async def emergency_close_all(self) -> list:
        return []

    async def initialize(self):
        pass


# ---------------------------------------------------------------------------
#  Rate Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window rate limiter for Binance (weight + order frequency)."""

    def __init__(self, max_weight_per_min: int = 1000,
                 max_orders_per_sec: int = 8):
        self._weight_window: deque = deque()
        self._order_window: deque = deque()
        self.max_weight = max_weight_per_min
        self.max_orders = max_orders_per_sec
        self._lock = asyncio.Lock()

    async def acquire(self, weight: int = 1):
        # Compute wait times under lock, then sleep outside lock to avoid blocking others.
        while True:
            weight_wait = 0.0
            order_wait = 0.0
            async with self._lock:
                now = time.time()

                # Purge stale entries
                while self._weight_window and now - self._weight_window[0][0] > 60:
                    self._weight_window.popleft()
                while self._order_window and now - self._order_window[0] > 1:
                    self._order_window.popleft()

                # Weight gate
                total_weight = sum(w for _, w in self._weight_window)
                if total_weight + weight > self.max_weight:
                    weight_wait = 60 - (now - self._weight_window[0][0]) + 0.1

                # Order-per-second gate
                if len(self._order_window) >= self.max_orders:
                    wait = 1.0 - (now - self._order_window[0])
                    if wait > 0:
                        order_wait = wait + 0.05

                # If no wait needed, record and return immediately
                if weight_wait <= 0 and order_wait <= 0:
                    self._weight_window.append((time.time(), weight))
                    self._order_window.append(time.time())
                    return

            # Sleep outside lock, then re-check
            total_sleep = max(weight_wait, order_wait)
            if weight_wait > 0:
                logging.warning(f"[RateLimit] weight cap — waiting {weight_wait:.1f}s")
            await asyncio.sleep(total_sleep)


# ---------------------------------------------------------------------------
#  Binance Executor
# ---------------------------------------------------------------------------

# Error codes where retrying is pointless
_FATAL_CODES = frozenset({-2019, -1111, -1116, -4131, -2015, -4028})


class BinanceExecutor(OrderExecutor):
    """Binance Futures USDT-M live order executor."""

    def __init__(self, session: aiohttp.ClientSession,
                 signer: BinanceSigner):
        self.session = session
        self.signer = signer
        self.base_url = FAPI_REST_BASE
        self._leverage_cache: dict = {}
        self._rate_limiter = RateLimiter()
        self._order_log: list = []
        self.max_capital = MAX_CAPITAL

    # ------------------------------------------------------------------
    #  Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self):
        # 1. Ping
        await self._ping()

        # 2. Position mode — must be One-Way
        mode = await self._get_position_mode()
        if mode.get('dualSidePosition'):
            raise RuntimeError(
                "Hedge Mode is active. "
                "Switch to One-Way Mode on Binance before starting."
            )

        # 3. Balance check
        balance = await self.get_balance()
        logging.info(f"[Executor] Exchange Balance: ${balance:,.2f}")
        if balance < 10.0:
            raise RuntimeError(f"Insufficient balance: ${balance:.2f}")

        # 4. Log existing positions
        positions = await self.get_positions()
        if positions:
            logging.warning(
                f"[Executor] {len(positions)} existing position(s): "
                f"{list(positions.keys())}"
            )

        logging.info("[Executor] BinanceExecutor initialised OK")

    # ------------------------------------------------------------------
    #  Order placement
    # ------------------------------------------------------------------

    async def open_order(self, symbol, side, qty, leverage) -> OrderResult:
        # Safety gate: max capital
        balance = await self.get_balance()
        if balance > self.max_capital:
            logging.critical(
                f"[Executor] Capital ceiling exceeded: "
                f"${balance:.2f} > ${self.max_capital:.2f}"
            )
            return OrderResult(success=False,
                               error_msg="MAX_CAPITAL_EXCEEDED")

        # Set leverage (cached)
        if self._leverage_cache.get(symbol) != leverage:
            if not await self._set_leverage(symbol, leverage):
                return OrderResult(success=False,
                                   error_msg="LEVERAGE_SET_FAILED")
            self._leverage_cache[symbol] = leverage

        # Market order
        order_side = 'BUY' if side == 'LONG' else 'SELL'
        result = await self._signed_request('POST', '/fapi/v1/order', {
            'symbol': symbol,
            'side': order_side,
            'type': 'MARKET',
            'quantity': str(qty),
            'newOrderRespType': 'RESULT',
        })
        return self._parse_order_response(result, qty)

    async def close_order(self, symbol, side, qty) -> OrderResult:
        close_side = 'SELL' if side == 'LONG' else 'BUY'
        result = await self._signed_request('POST', '/fapi/v1/order', {
            'symbol': symbol,
            'side': close_side,
            'type': 'MARKET',
            'quantity': str(qty),
            'reduceOnly': 'true',
            'newOrderRespType': 'RESULT',
        })
        return self._parse_order_response(result, qty)

    async def emergency_close_all(self) -> list:
        positions = await self.get_positions()
        results = []
        for sym, pos in positions.items():
            r = await self.close_order(sym, pos['type'], pos['qty'])
            results.append((sym, r))
            logging.critical(f"[KILL] EMERGENCY CLOSE: {sym} -> {r.status}")
        return results

    # ------------------------------------------------------------------
    #  Account queries
    # ------------------------------------------------------------------

    async def get_balance(self) -> float:
        result = await self._signed_request('GET', '/fapi/v2/account', {})
        if result:
            for asset in result.get('assets', []):
                if asset['asset'] == 'USDT':
                    return float(asset['availableBalance'])
        return 0.0

    async def get_positions(self) -> dict:
        result = await self._signed_request(
            'GET', '/fapi/v2/positionRisk', {}
        )
        positions: dict = {}
        if result and isinstance(result, list):
            for p in result:
                amt = float(p.get('positionAmt', 0))
                if abs(amt) > 0:
                    positions[p['symbol']] = {
                        'type': 'LONG' if amt > 0 else 'SHORT',
                        'qty': abs(amt),
                        'entry': float(p['entryPrice']),
                        'unrealized_pnl': float(p['unRealizedProfit']),
                        'leverage': int(p['leverage']),
                        'margin_type': p.get('marginType', ''),
                    }
        return positions

    # ------------------------------------------------------------------
    #  Configuration helpers
    # ------------------------------------------------------------------

    async def _set_leverage(self, symbol: str, leverage: int) -> bool:
        result = await self._signed_request('POST', '/fapi/v1/leverage', {
            'symbol': symbol,
            'leverage': leverage,
        })
        if result and 'leverage' in result:
            logging.info(
                f"[Executor] Leverage set: {symbol} -> {result['leverage']}x"
            )
            return True
        logging.error(f"[Executor] Leverage set failed: {symbol} -> {leverage}x")
        return False

    async def _get_position_mode(self) -> dict:
        return await self._signed_request(
            'GET', '/fapi/v1/positionSide/dual', {}
        ) or {}

    async def _ping(self):
        async with self.session.get(
            f"{self.base_url}/fapi/v1/ping",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                raise ConnectionError("Binance API ping failed")

    # ------------------------------------------------------------------
    #  Signed request with retry + rate-limit
    # ------------------------------------------------------------------

    async def _signed_request(self, method: str, path: str,
                              params: dict,
                              max_retries: int = 3) -> Optional[dict]:
        url = f"{self.base_url}{path}"

        for attempt in range(max_retries):
            await self._rate_limiter.acquire()
            signed = self.signer.sign_params(params.copy())
            headers = self.signer.headers

            try:
                if method == 'GET':
                    async with self.session.get(
                        url, params=signed, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        body = await resp.json()
                        self._log_api(method, path, signed, body, resp.status)
                        if resp.status == 200:
                            return body
                        if resp.status == 429:
                            await asyncio.sleep(min(30, 2 ** (attempt + 1)))
                            continue
                        # Retry GET on server errors (5xx)
                        if resp.status >= 500 and attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return body

                elif method == 'POST':
                    async with self.session.post(
                        url, data=signed, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        body = await resp.json()
                        self._log_api(method, path, signed, body, resp.status)
                        if resp.status == 200:
                            return body
                        if resp.status == 429:
                            wait = min(30, 2 ** (attempt + 1))
                            logging.warning(
                                f"[Executor] 429 on {path}. Waiting {wait}s"
                            )
                            await asyncio.sleep(wait)
                            continue
                        code = body.get('code', 0)
                        if code in _FATAL_CODES:
                            logging.error(
                                f"[Executor] Fatal error {code}: "
                                f"{body.get('msg')}"
                            )
                            return body
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return body

                elif method == 'PUT':
                    async with self.session.put(
                        url, data=signed, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        body = await resp.json()
                        self._log_api(method, path, signed, body, resp.status)
                        if resp.status == 200:
                            return body
                        code = body.get('code', 0)
                        if code in _FATAL_CODES:
                            return body
                        if attempt < max_retries - 1:
                            await asyncio.sleep(2 ** attempt)
                            continue
                        return body

                elif method == 'DELETE':
                    async with self.session.delete(
                        url, params=signed, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        body = await resp.json()
                        self._log_api(method, path, signed, body, resp.status)
                        return body

            except asyncio.TimeoutError:
                logging.warning(
                    f"[Executor] Timeout {path} "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logging.error(f"[Executor] Request failed {path}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)

        return None

    # ------------------------------------------------------------------
    #  Response parsing
    # ------------------------------------------------------------------

    def _parse_order_response(self, result: Optional[dict],
                              requested_qty: float) -> OrderResult:
        if not result:
            return OrderResult(success=False, error_msg="API_REQUEST_FAILED")

        status = result.get('status', '')

        if status == 'FILLED':
            return OrderResult(
                success=True,
                order_id=result.get('orderId'),
                avg_price=float(result.get('avgPrice', 0)),
                executed_qty=float(result.get('executedQty', 0)),
                commission=self._extract_commission(result),
                status='FILLED',
                raw_response=result,
            )

        if status == 'PARTIALLY_FILLED':
            filled = float(result.get('executedQty', 0))
            logging.warning(
                f"[Executor] Partial fill: "
                f"{result.get('symbol')} {filled}/{requested_qty}"
            )
            if filled > 0:
                return OrderResult(
                    success=True,
                    order_id=result.get('orderId'),
                    avg_price=float(result.get('avgPrice', 0)),
                    executed_qty=filled,
                    commission=self._extract_commission(result),
                    status='PARTIALLY_FILLED',
                    raw_response=result,
                )
            return OrderResult(success=False, status='NO_FILL',
                               error_msg="Partial fill with 0 qty")

        # REJECTED / EXPIRED / error body
        return OrderResult(
            success=False, status=status,
            error_code=result.get('code'),
            error_msg=result.get('msg', status or 'Unknown'),
            raw_response=result,
        )

    @staticmethod
    def _extract_commission(resp: dict) -> float:
        """Best-effort commission extraction from order response."""
        # RESULT response type does not always include cumCommission.
        try:
            return float(resp.get('cumCommission', 0))
        except (TypeError, ValueError):
            return 0.0

    # ------------------------------------------------------------------
    #  Audit log
    # ------------------------------------------------------------------

    def _log_api(self, method, path, params, response, status_code):
        safe_params = {k: v for k, v in params.items()
                       if k not in ('signature', 'timestamp')}
        entry = {
            'ts': datetime.utcnow().isoformat(),
            'method': method,
            'path': path,
            'params': safe_params,
            'status': status_code,
            'response': response,
        }
        self._order_log.append(entry)
        if len(self._order_log) > 1000:
            self._order_log = self._order_log[-500:]

        if '/order' in path:
            logging.info(
                f"[ORDER] {method} {path} "
                f"status={status_code} "
                f"resp={json.dumps(response, default=str)[:200]}"
            )
