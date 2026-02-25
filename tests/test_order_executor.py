"""Tests for order_executor.py — Paper & Binance executors."""
import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from order_executor import (
    OrderResult,
    PaperExecutor,
    BinanceExecutor,
    RateLimiter,
)
from binance_signer import BinanceSigner


# -----------------------------------------------------------------------
#  PaperExecutor
# -----------------------------------------------------------------------

class TestPaperExecutor:
    @pytest.fixture
    def executor(self):
        return PaperExecutor({"balance": 5000.0})

    @pytest.mark.asyncio
    async def test_open_order_always_succeeds(self, executor):
        r = await executor.open_order("BTCUSDT", "LONG", 0.01, 10)
        assert r.success is True
        assert r.status == "FILLED"
        assert r.executed_qty == 0.01
        assert r.avg_price == 0.0  # caller supplies price

    @pytest.mark.asyncio
    async def test_close_order_always_succeeds(self, executor):
        r = await executor.close_order("ETHUSDT", "SHORT", 0.5)
        assert r.success is True
        assert r.status == "FILLED"

    @pytest.mark.asyncio
    async def test_get_balance_returns_wallet(self, executor):
        bal = await executor.get_balance()
        assert bal == 5000.0

    @pytest.mark.asyncio
    async def test_get_positions_empty(self, executor):
        pos = await executor.get_positions()
        assert pos == {}

    @pytest.mark.asyncio
    async def test_emergency_close_all_empty(self, executor):
        result = await executor.emergency_close_all()
        assert result == []

    @pytest.mark.asyncio
    async def test_initialize_noop(self, executor):
        await executor.initialize()  # should not raise


# -----------------------------------------------------------------------
#  BinanceExecutor (mocked network)
# -----------------------------------------------------------------------

class TestBinanceExecutor:
    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        return session

    @pytest.fixture
    def executor(self, mock_session):
        signer = BinanceSigner("key", "secret")
        config = {"system": {"max_capital": 10000.0}}
        return BinanceExecutor(mock_session, config, signer)

    def _mock_response(self, session, status, body):
        """Helper: make session.get/post return a mock response."""
        resp = AsyncMock()
        resp.status = status
        resp.json = AsyncMock(return_value=body)
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.get = MagicMock(return_value=ctx)
        session.post = MagicMock(return_value=ctx)
        session.delete = MagicMock(return_value=ctx)
        return resp

    @pytest.mark.asyncio
    async def test_open_order_filled(self, executor, mock_session):
        # Mock get_balance call (first call) then order call (second call)
        balance_resp = AsyncMock()
        balance_resp.status = 200
        balance_resp.json = AsyncMock(return_value={
            "assets": [{"asset": "USDT", "availableBalance": "5000.0"}]
        })

        leverage_resp = AsyncMock()
        leverage_resp.status = 200
        leverage_resp.json = AsyncMock(return_value={"leverage": 10, "symbol": "BTCUSDT"})

        order_resp = AsyncMock()
        order_resp.status = 200
        order_resp.json = AsyncMock(return_value={
            "orderId": 12345,
            "status": "FILLED",
            "avgPrice": "43250.50",
            "executedQty": "0.010",
            "cumCommission": "0.17",
            "symbol": "BTCUSDT",
        })

        call_count = {"n": 0}
        get_ctx = AsyncMock()
        get_ctx.__aenter__ = AsyncMock(return_value=balance_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=False)

        def make_post_ctx(*args, **kwargs):
            call_count["n"] += 1
            ctx = AsyncMock()
            if call_count["n"] == 1:
                ctx.__aenter__ = AsyncMock(return_value=leverage_resp)
            else:
                ctx.__aenter__ = AsyncMock(return_value=order_resp)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.post = MagicMock(side_effect=make_post_ctx)

        r = await executor.open_order("BTCUSDT", "LONG", 0.01, 10)
        assert r.success is True
        assert r.avg_price == 43250.50
        assert r.executed_qty == 0.01
        assert r.order_id == 12345

    @pytest.mark.asyncio
    async def test_open_order_rejected(self, executor, mock_session):
        balance_resp = AsyncMock()
        balance_resp.status = 200
        balance_resp.json = AsyncMock(return_value={
            "assets": [{"asset": "USDT", "availableBalance": "5000.0"}]
        })

        error_resp = AsyncMock()
        error_resp.status = 400
        error_resp.json = AsyncMock(return_value={
            "code": -2019,
            "msg": "Margin is insufficient."
        })

        get_ctx = AsyncMock()
        get_ctx.__aenter__ = AsyncMock(return_value=balance_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=False)

        post_ctx = AsyncMock()
        post_ctx.__aenter__ = AsyncMock(return_value=error_resp)
        post_ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.post = MagicMock(return_value=post_ctx)

        # Pre-set leverage cache to skip leverage call
        executor._leverage_cache["BTCUSDT"] = 10

        r = await executor.open_order("BTCUSDT", "SHORT", 0.01, 10)
        assert r.success is False
        assert r.error_code == -2019

    @pytest.mark.asyncio
    async def test_close_order_filled(self, executor, mock_session):
        order_resp = AsyncMock()
        order_resp.status = 200
        order_resp.json = AsyncMock(return_value={
            "orderId": 99999,
            "status": "FILLED",
            "avgPrice": "3100.25",
            "executedQty": "0.5",
            "cumCommission": "0.06",
        })
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=order_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=ctx)

        r = await executor.close_order("ETHUSDT", "LONG", 0.5)
        assert r.success is True
        assert r.avg_price == 3100.25

    @pytest.mark.asyncio
    async def test_get_balance(self, executor, mock_session):
        self._mock_response(mock_session, 200, {
            "assets": [
                {"asset": "BNB", "availableBalance": "1.0"},
                {"asset": "USDT", "availableBalance": "7500.50"},
            ]
        })
        bal = await executor.get_balance()
        assert bal == 7500.50

    @pytest.mark.asyncio
    async def test_get_positions(self, executor, mock_session):
        self._mock_response(mock_session, 200, [
            {"symbol": "BTCUSDT", "positionAmt": "0.010", "entryPrice": "43000",
             "unRealizedProfit": "25.0", "leverage": "10", "marginType": "cross"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0",
             "unRealizedProfit": "0", "leverage": "10", "marginType": "cross"},
            {"symbol": "SOLUSDT", "positionAmt": "-5.0", "entryPrice": "150",
             "unRealizedProfit": "-10.0", "leverage": "5", "marginType": "isolated"},
        ])
        pos = await executor.get_positions()
        assert len(pos) == 2
        assert pos["BTCUSDT"]["type"] == "LONG"
        assert pos["BTCUSDT"]["qty"] == 0.01
        assert pos["SOLUSDT"]["type"] == "SHORT"
        assert pos["SOLUSDT"]["qty"] == 5.0

    @pytest.mark.asyncio
    async def test_max_capital_blocks_order(self, executor, mock_session):
        """If exchange balance exceeds max_capital, order is refused."""
        self._mock_response(mock_session, 200, {
            "assets": [{"asset": "USDT", "availableBalance": "99999.0"}]
        })
        r = await executor.open_order("BTCUSDT", "LONG", 0.01, 10)
        assert r.success is False
        assert r.error_msg == "MAX_CAPITAL_EXCEEDED"


# -----------------------------------------------------------------------
#  RateLimiter
# -----------------------------------------------------------------------

class TestRateLimiter:
    @pytest.mark.asyncio
    async def test_acquire_does_not_block_under_limit(self):
        rl = RateLimiter(max_weight_per_min=100, max_orders_per_sec=10)
        start = time.time()
        for _ in range(5):
            await rl.acquire()
        elapsed = time.time() - start
        assert elapsed < 0.5  # should be near-instant

    @pytest.mark.asyncio
    async def test_order_frequency_throttles(self):
        rl = RateLimiter(max_weight_per_min=9999, max_orders_per_sec=2)
        start = time.time()
        # First 2 should be instant, 3rd should wait ~1s
        await rl.acquire()
        await rl.acquire()
        await rl.acquire()
        elapsed = time.time() - start
        assert elapsed >= 0.5  # throttled


# -----------------------------------------------------------------------
#  OrderResult dataclass
# -----------------------------------------------------------------------

class TestOrderResult:
    def test_defaults(self):
        r = OrderResult(success=True)
        assert r.success is True
        assert r.order_id is None
        assert r.avg_price == 0.0
        assert r.executed_qty == 0.0
        assert r.commission == 0.0
        assert r.status == ""
        assert r.error_code is None
        assert r.error_msg == ""
        assert r.raw_response is None

    def test_filled_result(self):
        r = OrderResult(
            success=True,
            order_id=123,
            avg_price=43000.0,
            executed_qty=0.01,
            commission=0.17,
            status="FILLED",
        )
        assert r.success
        assert r.avg_price == 43000.0

    def test_error_result(self):
        r = OrderResult(
            success=False,
            error_code=-2019,
            error_msg="Margin insufficient",
        )
        assert not r.success
        assert r.error_code == -2019
