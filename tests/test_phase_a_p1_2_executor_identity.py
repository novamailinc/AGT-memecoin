#!/usr/bin/env python3
"""Phase A regression (P1-2, 2026-11-22): MainnetExecutor.connect()
with a wallet key whose pubkey != expected_pubkey must raise
BrokerError(CREDENTIALS_INVALID) — before the fix it raised a bare
NameError (CREDENTIALS_INVALID was never imported into
mainnet_executor.py). No live key, no network: fake signer/RPC +
a deployment marker, as in test_live_executor.py.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import BrokerError, CREDENTIALS_INVALID
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, deployment_marker_path)

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


def _make_keypair():
    from solders.keypair import Keypair
    return Keypair()


class _StaticProvider:
    """WalletKeyProvider stub returning a fixed secret."""

    def __init__(self, secret):
        self._secret = secret

    def get_key(self, chain_name):
        return self._secret

    def describe_source(self, chain_name):
        return "test-static"


class _FakeRPC:
    """Minimal MainnetRPC stand-in."""

    def __init__(self):
        self.health = "ok"

    def get_health(self):
        return self.health

    def get_slot(self):
        return 123456

    def get_balance(self, pubkey):
        return 1_000_000_000

    def get_token_account_balance(self, pubkey):
        return {"amount": "0", "decimals": 6, "uiAmount": 0.0}


def main():
    kp = _make_keypair()
    # The signer takes a JSON array of 64 ints (Solana CLI id.json
    # format) or base58 (signer.py); JSON needs no extra dependency.
    secret = json.dumps(list(bytes(kp)))
    provider = _StaticProvider(secret)
    pubkey = str(kp.pubkey())
    other = str(_make_keypair().pubkey())
    assert pubkey != other
    with tempfile.TemporaryDirectory() as tmp:
        live_dir = os.path.join(tmp, "live")
        os.makedirs(live_dir)
        marker_path = deployment_marker_path(tmp)
        with open(marker_path, "w") as f:
            json.dump({"machine": "test-vm", "armed": False}, f)

        # Mismatched expected pubkey: BrokerError with code
        # CREDENTIALS_INVALID — never a bare NameError.
        ex = MainnetExecutor(key_provider=provider, rpc=_FakeRPC(),
                             live_home=tmp, expected_pubkey=other)
        try:
            ex.connect()
            raise AssertionError("connected with mismatched key")
        except NameError:
            raise AssertionError(
                "identity mismatch raised NameError, not BrokerError")
        except BrokerError as e:
            check("identity mismatch raises BrokerError",
                  e.code == CREDENTIALS_INVALID)
            check("identity mismatch names the pubkey check",
                  "does not match" in str(e))

        # Matching expected pubkey: connects.
        ex2 = MainnetExecutor(key_provider=provider, rpc=_FakeRPC(),
                              live_home=tmp, expected_pubkey=pubkey)
        check("matching pubkey connects",
              ex2.connect() == pubkey)

    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
