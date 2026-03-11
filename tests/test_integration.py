"""Integration tests for SharkTrader + Executor lifecycle.

Tests the full open_position → update_pnl → close_position flow,
sync_balance, and reconcile_positions with mock executors.
"""
import asyncio
import os
import sqlite3
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core_trader import SharkTrader
from order_executor import OrderResult, PaperExecutor, OrderExecutor


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _make_trader_async(executor=None, balance=10000.0):
    """Create a SharkTrader with a real DB (temp) and optional executor."""
    config = {
        'system': {'max_positions': 5},
        'telegram': {'enabled': False},
    }
    # Use temp DB
    t = SharkTrader.__new__(SharkTrader)
    t.sys_cfg = config['system']
    t.tg_cfg = config['telegram']
    t.db_path = "/tmp/test_shark_trader.db"
    t.fee_rate = 0.0004
    t.margin_ratio = 0.1
    t.global_pnl_stop_limit = -0.10
    t.lock = asyncio.Lock()
    t.positions = {}
    t.wallet = {"balance": balance}
    t.session = None
    t.loss_streak = {}
    t._background_tasks = set()
    t.db_queue = asyncio.Queue()
    t.db_worker_task = None
    t.executor = executor or PaperExecutor(t.wallet)

    # Init DB
    if os.path.exists(t.db_path):
        os.remove(t.db_path)
    t._init_db()
    # Set initial balance in DB
    with sqlite3.connect(t.db_path) as conn:
        conn.execute("UPDATE wallet SET balance=? WHERE id=1", (balance,))

    return t


class MockIntel:
    """Minimal mock of MarketIntelligence for open_position."""
    symbol_filters = {'BTCUSDT': {'stepSize': 0.001}, 'ETHUSDT': {'stepSize': 0.01}}


# ---------------------------------------------------------------------------
#  open_position tests
# ---------------------------------------------------------------------------

class TestOpenPositionIntegration:

    @pytest.fixture
    def intel(self):
        return MockIntel()

    @pytest.mark.asyncio
    async def test_paper_open_uses_market_price(self, intel):
        """PaperExecutor returns avg_price=0 -> trader uses market price."""
        t = _make_trader_async()
        t.start_db_worker()
        try:
            details = {'mode': 'APEX-REVERSAL', 'max_price': 50000.0, 'vwap': 2.0, 'rsi5': 30, 'rsi15': 35}
            ok = await t.open_position('BTCUSDT', 'LONG', 500.0, 1, 'APEX-REVERSAL', 50000.0, intel, details)
            assert ok is True
            assert 'BTCUSDT' in t.positions
            assert t.positions['BTCUSDT']['entry'] == 50000.0
            assert t.positions['BTCUSDT']['type'] == 'LONG'
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_live_open_uses_fill_price(self, intel):
        """BinanceExecutor returns a fill price -> trader uses it."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.open_order = AsyncMock(return_value=OrderResult(
            success=True, avg_price=50100.0, executed_qty=0.02, status='FILLED'
        ))
        t = _make_trader_async(executor=mock_exec)
        t.start_db_worker()
        try:
            details = {'mode': 'WHALE-FORCE', 'max_price': 50000.0}
            ok = await t.open_position('BTCUSDT', 'LONG', 500.0, 1, 'WHALE-FORCE', 50000.0, intel, details)
            assert ok is True
            assert t.positions['BTCUSDT']['entry'] == 50100.0
            assert t.positions['BTCUSDT']['qty'] == 0.02
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_open_fails_on_executor_reject(self, intel):
        """If executor rejects, position is NOT created."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.open_order = AsyncMock(return_value=OrderResult(
            success=False, error_msg="INSUFFICIENT_MARGIN"
        ))
        t = _make_trader_async(executor=mock_exec)
        t.start_db_worker()
        try:
            details = {'mode': 'APEX-REVERSAL', 'max_price': 50000.0}
            ok = await t.open_position('BTCUSDT', 'LONG', 500.0, 1, 'APEX-REVERSAL', 50000.0, intel, details)
            assert ok is False
            assert 'BTCUSDT' not in t.positions
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_cannot_open_duplicate(self, intel):
        """Cannot open same symbol twice."""
        t = _make_trader_async()
        t.start_db_worker()
        try:
            details = {'mode': 'APEX-REVERSAL', 'max_price': 50000.0}
            ok1 = await t.open_position('BTCUSDT', 'LONG', 500.0, 1, 'APEX-REVERSAL', 50000.0, intel, details)
            ok2 = await t.open_position('BTCUSDT', 'SHORT', 500.0, 1, 'APEX-REVERSAL', 50000.0, intel, details)
            assert ok1 is True
            assert ok2 is False
        finally:
            t.stop()


# ---------------------------------------------------------------------------
#  close_position tests
# ---------------------------------------------------------------------------

class TestClosePositionIntegration:

    @pytest.mark.asyncio
    async def test_paper_close_updates_wallet(self):
        """Full lifecycle: open -> close -> wallet updated."""
        t = _make_trader_async(balance=10000.0)
        t.start_db_worker()
        try:
            intel = MockIntel()
            details = {'mode': 'APEX-REVERSAL', 'max_price': 100.0}
            await t.open_position('ETHUSDT', 'LONG', 500.0, 1, 'APEX-REVERSAL', 100.0, intel, details)
            assert 'ETHUSDT' in t.positions
            initial_bal = t.wallet['balance']

            # Close at profit (price went up 5%)
            await t.close_position('ETHUSDT', 105.0, 'Test Close')
            # Allow DB task to process
            await asyncio.sleep(0.2)

            assert 'ETHUSDT' not in t.positions
            # Balance should have changed (profit minus fees)
            assert t.wallet['balance'] != initial_bal
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_close_executor_fail_resets_is_closing(self):
        """If executor.close_order fails, is_closing resets to False."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.open_order = AsyncMock(return_value=OrderResult(
            success=True, avg_price=100.0, executed_qty=1.0, status='FILLED'
        ))
        mock_exec.close_order = AsyncMock(return_value=OrderResult(
            success=False, error_msg="NETWORK_ERROR"
        ))
        t = _make_trader_async(executor=mock_exec)
        t.start_db_worker()
        try:
            intel = MockIntel()
            details = {'mode': 'APEX-REVERSAL', 'max_price': 100.0}
            await t.open_position('ETHUSDT', 'LONG', 500.0, 1, 'APEX-REVERSAL', 100.0, intel, details)

            await t.close_position('ETHUSDT', 105.0, 'Test Close')

            # Position should still exist, is_closing reset
            assert 'ETHUSDT' in t.positions
            assert t.positions['ETHUSDT'].get('is_closing') is False
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_close_nonexistent_is_noop(self):
        """Closing a symbol that doesn't exist does nothing."""
        t = _make_trader_async()
        t.start_db_worker()
        try:
            initial_bal = t.wallet['balance']
            await t.close_position('XYZUSDT', 100.0, 'Test')
            assert t.wallet['balance'] == initial_bal
        finally:
            t.stop()


# ---------------------------------------------------------------------------
#  sync_balance tests
# ---------------------------------------------------------------------------

class TestSyncBalanceIntegration:

    @pytest.mark.asyncio
    async def test_paper_mode_is_noop(self):
        """sync_balance does nothing in paper mode."""
        t = _make_trader_async()
        t.start_db_worker()
        try:
            initial_bal = t.wallet['balance']
            await t.sync_balance()
            assert t.wallet['balance'] == initial_bal
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_live_mode_corrects_drift(self):
        """sync_balance corrects local balance when exchange differs >1%."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.get_balance = AsyncMock(return_value=9500.0)
        # Not a PaperExecutor, so sync_balance should run
        t = _make_trader_async(executor=mock_exec, balance=10000.0)
        t.start_db_worker()
        try:
            await t.sync_balance()
            # 5% drift > 1% threshold -> corrected
            assert t.wallet['balance'] == 9500.0
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_live_mode_small_drift_ignored(self):
        """sync_balance ignores drift < 1%."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.get_balance = AsyncMock(return_value=10050.0)  # 0.5% drift
        t = _make_trader_async(executor=mock_exec, balance=10000.0)
        t.start_db_worker()
        try:
            await t.sync_balance()
            # 0.5% drift < 1% threshold -> NOT corrected
            assert t.wallet['balance'] == 10000.0
        finally:
            t.stop()


# ---------------------------------------------------------------------------
#  reconcile_positions tests
# ---------------------------------------------------------------------------

class TestReconcilePositionsIntegration:

    @pytest.mark.asyncio
    async def test_paper_mode_is_noop(self):
        """reconcile_positions does nothing in paper mode."""
        t = _make_trader_async()
        t.start_db_worker()
        try:
            await t.reconcile_positions()
            assert len(t.positions) == 0
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_untracked_exchange_position_registered(self):
        """Position on exchange but not local gets registered."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.get_positions = AsyncMock(return_value={
            'SOLUSDT': {'type': 'LONG', 'qty': 5.0, 'entry': 150.0,
                        'leverage': 10, 'unrealized_pnl': 10.0},
        })
        t = _make_trader_async(executor=mock_exec)
        t.start_db_worker()
        try:
            await t.reconcile_positions()
            assert 'SOLUSDT' in t.positions
            assert t.positions['SOLUSDT']['type'] == 'LONG'
            assert t.positions['SOLUSDT']['qty'] == 5.0
            assert t.positions['SOLUSDT']['details']['mode'] == 'RECOVERED'
        finally:
            t.stop()

    @pytest.mark.asyncio
    async def test_ghost_position_removed(self):
        """Position in local but not on exchange gets removed."""
        mock_exec = AsyncMock(spec=OrderExecutor)
        mock_exec.get_positions = AsyncMock(return_value={})
        t = _make_trader_async(executor=mock_exec)
        t.positions['GHOSTUSDT'] = {
            'entry': 100.0, 'type': 'SHORT', 'qty': 1.0,
            'entry_margin': 10.0, 'lev': 10, 'start_time': time.time(),
            'details': {'mode': 'WHALE-FORCE'},
        }
        t.start_db_worker()
        try:
            await t.reconcile_positions()
            assert 'GHOSTUSDT' not in t.positions
        finally:
            t.stop()


# ---------------------------------------------------------------------------
#  PaperExecutor wallet sharing test
# ---------------------------------------------------------------------------

class TestPaperExecutorWalletSharing:

    @pytest.mark.asyncio
    async def test_wallet_is_shared_reference(self):
        """PaperExecutor(self.wallet) shares the same dict object."""
        t = _make_trader_async()
        # The default constructor passes self.wallet to PaperExecutor
        assert t.executor.wallet is t.wallet
        # Mutation in trader is visible in executor
        t.wallet['balance'] = 5000.0
        bal = await t.executor.get_balance()
        assert bal == 5000.0


# ---------------------------------------------------------------------------
#  DB worker lazy start test
# ---------------------------------------------------------------------------

class TestDBWorkerLazyStart:

    def test_db_worker_not_started_in_init(self):
        """DB worker task should be None after __init__ (not auto-started)."""
        t = _make_trader_async()
        assert t.db_worker_task is None

    @pytest.mark.asyncio
    async def test_start_db_worker_creates_task(self):
        """start_db_worker() creates the task."""
        t = _make_trader_async()
        t.start_db_worker()
        assert t.db_worker_task is not None
        assert not t.db_worker_task.done()
        t.stop()

    @pytest.mark.asyncio
    async def test_ensure_db_worker_auto_starts(self):
        """_ensure_db_worker() auto-starts if not running."""
        t = _make_trader_async()
        assert t.db_worker_task is None
        await t._ensure_db_worker()
        assert t.db_worker_task is not None
        assert not t.db_worker_task.done()
        t.stop()


# Cleanup
@pytest.fixture(autouse=True)
def cleanup_test_db():
    yield
    try:
        os.remove("/tmp/test_shark_trader.db")
    except FileNotFoundError:
        pass
