#!/usr/bin/env python3
"""Step 0 tests for the mainnet live executor (plain script — run
with the repo venv python, never pytest):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_live_executor.py

Covers: MainnetRPC allowlist (Helius + public mainnet-beta only),
MainnetExecutor construction rules, deployment-marker VM refusal,
dry-run default + arming rules, token-balance missing-ATA handling.
No real network calls anywhere — fake RPC + fake key provider only.
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import rpc as rpc_mod
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, associated_token_address,
    deployment_marker_path)
from broker.memecoin.wallet import FileKeyProvider

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

    def __init__(self, health="ok", balance=1_000_000_000,
                 token_balances=None, raise_token_error=False):
        self.health = health
        self.balance = balance
        self.token_balances = token_balances or {}
        self.raise_token_error = raise_token_error
        self.sent = []

    def get_health(self):
        return self.health

    def get_slot(self):
        return 123456

    def get_balance(self, pubkey):
        return self.balance

    def get_token_account_balance(self, pubkey):
        if self.raise_token_error:
            raise BrokerError("rpc_error", "account not found",
                              detail={"reason": "rpc_error"})
        return {"amount": str(self.token_balances.get(pubkey, 0)),
                "decimals": 6, "uiAmount": 0.0}

    def send_transaction(self, b64_tx):
        self.sent.append(b64_tx)
        return "fakesig"

    def get_signature_statuses(self, sigs):
        return [{"confirmationStatus": "confirmed", "err": None}
                for _ in sigs]


def _executor(tmp_home, provider, rpc, **kw):
    return MainnetExecutor(key_provider=provider, rpc=rpc,
                           live_home=tmp_home, **kw)


def main():
    kp = _make_keypair()
    secret = json.dumps(list(bytes(kp.to_bytes())))
    provider = _StaticProvider(secret)
    pubkey = str(kp.pubkey())

    # -- MainnetRPC allowlist -----------------------------------------
    r = rpc_mod.MainnetRPC(
        "https://mainnet.helius-rpc.com/?api-key=testkey")
    check("allowlist accepts Helius mainnet host",
          r.url.startswith("https://mainnet.helius-rpc.com/"))
    r2 = rpc_mod.MainnetRPC("https://api.mainnet-beta.solana.com")
    check("allowlist accepts public mainnet-beta",
          r2.url == "https://api.mainnet-beta.solana.com")
    for bad in ("https://api.devnet.solana.com",
                "https://example.com",
                "https://mainnet.helius-rpc.com.evil.com",
                ""):
        try:
            rpc_mod.MainnetRPC(bad)
            raise AssertionError("allowlist accepted %r" % bad)
        except BrokerError:
            pass
    check("allowlist refuses devnet / random / empty hosts", True)

    # -- construction rules -------------------------------------------
    try:
        MainnetExecutor(key_provider=None, rpc=_FakeRPC())
        raise AssertionError("accepted None key provider")
    except BrokerError:
        pass
    check("executor refuses None key provider", True)
    try:
        MainnetExecutor(key_provider=provider)
        raise AssertionError("accepted no RPC endpoint")
    except BrokerError:
        pass
    check("executor refuses missing RPC endpoint", True)

    with tempfile.TemporaryDirectory() as tmp:
        # No deployment marker anywhere under tmp.
        ex = _executor(tmp, provider, _FakeRPC())
        check("dry_run defaults True", ex.dry_run is True)
        try:
            ex.connect()
            raise AssertionError("connected without deployment marker")
        except BrokerError as e:
            check("connect refuses without deployment marker",
                  "deployment marker" in str(e))

        # Marker present but not armed.
        live_dir = os.path.join(tmp, "live")
        os.makedirs(live_dir)
        marker_path = deployment_marker_path(tmp)
        with open(marker_path, "w") as f:
            json.dump({"machine": "test-vm", "armed": False}, f)
        got = ex.connect()
        check("connect works with deployment marker", got == pubkey)
        check("wallet pubkey recorded", ex.wallet_pubkey == pubkey)
        try:
            ex.arm_live()
            raise AssertionError("armed without armed:true marker")
        except BrokerError:
            pass
        check("arm_live refuses unarmed marker", True)
        check("still dry-run after refused arm", ex.dry_run is True)
        check("balance read", ex.get_balance_sol() == 1.0)

        # Missing ATA counts as zero.
        check("missing token account counts as zero",
              ex.get_token_balance(
                  "So11111111111111111111111111111111111111112") == 0)
        # RPC error on token balance also counts as zero.
        ex2 = _executor(tmp, provider,
                        _FakeRPC(raise_token_error=True))
        ex2.connect()
        check("token-balance RPC error counts as zero",
              ex2.get_token_balance(
                  "So11111111111111111111111111111111111111112") == 0)

        # Armed marker -> arm works -> disarm restores dry-run.
        with open(marker_path, "w") as f:
            json.dump({"machine": "test-vm", "armed": True}, f)
        ex.arm_live()
        check("arm_live works with armed:true marker",
              ex.dry_run is False)
        ex.disarm()
        check("disarm restores dry-run", ex.dry_run is True)

        # Malformed marker refuses.
        with open(marker_path, "w") as f:
            f.write("{not json")
        try:
            ex.connect()
            raise AssertionError("connected with malformed marker")
        except BrokerError:
            pass
        check("malformed marker refuses", True)
        with open(marker_path, "w") as f:
            json.dump({"armed": True}, f)
        try:
            ex.connect()
            raise AssertionError("connected with nameless marker")
        except BrokerError:
            pass
        check("marker without machine name refuses", True)

    # -- ATA derivation -------------------------------------------------
    ata = associated_token_address(
        pubkey, "So11111111111111111111111111111111111111112")
    check("ATA derivation deterministic",
          ata == associated_token_address(
              pubkey, "So11111111111111111111111111111111111111112"))
    check("ATA differs per mint",
          ata != associated_token_address(
              pubkey, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"))

    # -- FileKeyProvider still refuses bad files (plan SS3) ------------
    with tempfile.TemporaryDirectory() as tmp:
        bad = os.path.join(tmp, "key.json")
        with open(bad, "w") as f:
            f.write(secret)
        os.chmod(bad, 0o644)
        try:
            FileKeyProvider(bad).get_key("solana")
            raise AssertionError("loaded group-readable key file")
        except BrokerError:
            pass
    check("FileKeyProvider refuses 0644 key file", True)

    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
