import json
import os
import pytest
import tempfile


@pytest.fixture
def test_config():
    """Return parsed test config dict."""
    example = os.path.join(os.path.dirname(__file__), '..', 'shark_config.example.json')
    with open(example, 'r') as f:
        return json.load(f)


@pytest.fixture
def tmp_db(tmp_path):
    """Provide a temporary DB path for SharkTrader tests."""
    return str(tmp_path / "test_vault.db")
