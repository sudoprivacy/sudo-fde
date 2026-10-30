"""Explicitly opted-in, read-only external contract acceptance. No paid calls."""
import os

import pytest

from fde_server.config import Settings
from fde_server.router import SudoRouterClient


@pytest.mark.router_live
def test_real_router_contract_read_only():
    account_id = os.environ.get("FDE_LIVE_ROUTER_ACCOUNT")
    if not account_id:
        pytest.skip("Real Router deferred by user; set FDE_LIVE_ROUTER_ACCOUNT for read-only acceptance")
    client = SudoRouterClient(Settings())
    capabilities = client.capabilities()
    assert not capabilities.get("simulation")
    account = client.account(account_id)
    assert account["account_id"] == account_id
    assert isinstance(account["available_micros"], int)
    assert account["models"]
