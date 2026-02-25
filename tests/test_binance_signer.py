"""Tests for binance_signer.py — HMAC-SHA256 signing utility."""
import hmac
import hashlib
from urllib.parse import urlencode

import pytest
from binance_signer import BinanceSigner


@pytest.fixture
def signer():
    return BinanceSigner(api_key="test_key_123", api_secret="test_secret_456")


class TestSignParams:
    def test_adds_timestamp(self, signer):
        result = signer.sign_params({"symbol": "BTCUSDT"})
        assert "timestamp" in result
        assert isinstance(result["timestamp"], int)
        assert result["timestamp"] > 1_000_000_000_000  # millisecond epoch

    def test_adds_recv_window(self, signer):
        result = signer.sign_params({"symbol": "BTCUSDT"})
        assert result["recvWindow"] == 5000

    def test_adds_signature(self, signer):
        result = signer.sign_params({"symbol": "BTCUSDT"})
        assert "signature" in result
        assert len(result["signature"]) == 64  # SHA256 hex digest

    def test_hmac_sha256_is_correct(self, signer):
        params = {"symbol": "BTCUSDT", "side": "BUY"}
        result = signer.sign_params(params)

        # Reconstruct expected signature manually
        check_params = {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "timestamp": result["timestamp"],
            "recvWindow": 5000,
        }
        query_string = urlencode(check_params)
        expected = hmac.new(
            b"test_secret_456",
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        assert result["signature"] == expected

    def test_does_not_mutate_original_params(self, signer):
        original = {"symbol": "ETHUSDT"}
        _ = signer.sign_params(original)
        # Original dict should NOT have timestamp/signature
        assert "timestamp" not in original
        assert "signature" not in original

    def test_preserves_all_original_keys(self, signer):
        original = {"symbol": "SOLUSDT", "leverage": 10, "type": "MARKET"}
        result = signer.sign_params(original)
        for k, v in original.items():
            assert result[k] == v


class TestHeaders:
    def test_contains_api_key(self, signer):
        h = signer.headers
        assert h == {"X-MBX-APIKEY": "test_key_123"}

    def test_returns_new_dict_each_call(self, signer):
        h1 = signer.headers
        h2 = signer.headers
        assert h1 == h2
        assert h1 is not h2
