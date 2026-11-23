"""Existing shared-account opt-in reaches actual backend constructor offline."""
import time
from types import SimpleNamespace

import pytest
from agent.tools import backend
from broker.memecoin.adapter import MemecoinAdapter
from tests.test_broker_memecoin import FakeMarket, _req
from tests.test_paper_account_instances import account
from daemon.launch_scanner import paper_account as pa


class TestSharedBackendRoute:
    def test_existing_flag_routes_real_factory_and_downgrade_refuses(self, account, tmp_path, monkeypatch):
        cfg=SimpleNamespace(mode='dry_run',broker=SimpleNamespace(provider='memecoin',
             memecoin=SimpleNamespace(data_dir=str(tmp_path/'native'),symbols={'BONK':'MINT'})))
        monkeypatch.setattr(backend,'app_config',lambda:cfg)
        monkeypatch.setenv('PAPER_ACCOUNT_ENABLED','1')
        a=backend._build_adapter()
        assert isinstance(a,MemecoinAdapter) and a._shared_account is pa
        market=FakeMarket({'SOL':100.,'BONK':.0001})
        market.register('BONK','MINT')
        a._market=market
        a._fx_observer=lambda:{'usd_per_sol':100.,'observed_at':time.time(),'source':'fixture'}
        a.submit_order(_req('BONK','BUY',.02,'entry'))
        assert pa.snapshot()['cash']==8.
        monkeypatch.delenv('PAPER_ACCOUNT_ENABLED')
        with pytest.raises(ValueError,match='requires authoritative'):
            backend._build_adapter()

    def test_unbound_default_stays_standalone(self, account, tmp_path, monkeypatch):
        monkeypatch.delenv('PAPER_ACCOUNT_ENABLED',raising=False)
        cfg=SimpleNamespace(mode='dry_run',broker=SimpleNamespace(provider='memecoin',
             memecoin=SimpleNamespace(data_dir=str(tmp_path/'native'))))
        monkeypatch.setattr(backend,'app_config',lambda:cfg)
        assert backend._build_adapter()._shared_account is None
