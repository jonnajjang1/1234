"""Binance Futures HMAC-SHA256 request signing."""
import hmac
import hashlib
import time
from urllib.parse import urlencode


class BinanceSigner:
    """Stateless HMAC-SHA256 signer for Binance Futures API."""

    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret.encode('utf-8')

    def sign_params(self, params: dict) -> dict:
        """Return a NEW dict with timestamp + HMAC signature appended.
        The caller's original dict is NOT mutated."""
        signed = dict(params)
        signed['timestamp'] = int(time.time() * 1000)
        signed['recvWindow'] = 5000
        query_string = urlencode(signed)
        signature = hmac.new(
            self.api_secret,
            query_string.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()
        signed['signature'] = signature
        return signed

    @property
    def headers(self) -> dict:
        return {'X-MBX-APIKEY': self.api_key}
