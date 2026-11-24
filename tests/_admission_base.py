"""Runtime-binding CONTRACT tests for live admission (directive 22: no unqualified lane can be armed).
New file, unittest style (runs under pytest too). Offline/inert: stub signer/RPC/Jupiter, temp registry home,
no network. Each test states the property the integrated system must have. Tests marked GAP are expected
RED on a tree that only checks the registry state label; they are the acceptance bar for wiring
core.qualification_admission.admit() into arm_live and the send path. Fixtures write registry JSON directly
ONLY to model a forged/edited local file; nothing here authorizes anything."""
import ast, os, tempfile, unittest
from types import SimpleNamespace
from unittest import mock

from broker import BrokerError
from broker.memecoin.execution import mainnet_executor as M
from core.qualification import Registry

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
BIND = {'strategy_version': 'v', 'config_hash': 'h', 'data_period': 'p'}


class Quote(Exception):
    pass


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {'FOREX_AGENT_HOME': self.home}); p.start(); self.addCleanup(p.stop)
        self.marker = {'machine': 'inert-test', 'armed': True}
        self.calls = []
        calls = self.calls

        class Signer:
            def __init__(s, *a, **k): calls.append('signer')
            def pubkey_str(s): return 'INERT'
            def keypair(s): raise AssertionError('sign boundary reached')

        class Prov:
            def get_key(s, *a): raise AssertionError('key read')

        class Jup:
            def get_quote(s, *a, **k): calls.append('quote'); raise Quote()

        for target, val in ((M.signer_mod, 'DevnetSigner'),):
            p = mock.patch.object(target, val, Signer); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(M, 'read_deployment_marker', lambda *a: dict(self.marker)); p.start(); self.addCleanup(p.stop)
        rpc = SimpleNamespace(get_health=lambda: 'ok', get_slot=lambda: 1)
        self.ex = M.MainnetExecutor(key_provider=Prov(), rpc=rpc, jupiter=Jup())
        self.ex.connect()

    def write_state(self, state, evidence=None):
        Registry(self.home)._write({'v': 1, 'state': state, 'binding': BIND, 'evidence': evidence or {}, 'audit': []})


