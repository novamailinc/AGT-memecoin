#!/usr/bin/env python3
"""Step 2 tests for execute_swap (plain script):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_live_swap.py

Covers, with a fake Jupiter + fake RPC (zero real network calls):
happy-path buy records executed amounts from balances; dry-run
never sends; over-cap price impact / no-route / fee-over-cap are
skips (no send); failed simulate never sends; confirm timeout is
UNKNOWN with the signature attached and is never blind-retried
(send called exactly once); a swap tx failing the sanity check
never sends.
"""

import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import tx as tx_mod
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT, associated_token_address)

_PASSED = 0


def check(name, cond):
    global _PASSED
    if not cond:
        raise AssertionError("FAIL: %s" % name)
    _PASSED += 1
    print("PASS: %s" % name)


def _b58encode(raw: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + (out or "")


def _fake_blockhash() -> str:
    return _b58encode(bytes(range(32)))


class _StaticProvider:
    def __init__(self, secret):
        self._secret = secret

    def get_key(self, chain_name):
        return self._secret

    def describe_source(self, chain_name):
        return "test-static"


class _FakeJupiter:
    """Canned Jupiter: quote + swap tx built from a v0 tx."""

    def __init__(self, kp, quote=None, swap_b64=None,
                 quote_error=None):
        self._kp = kp
        self._quote = quote or {
            "outAmount": "1000000", "priceImpactPct": "0.5"}
        self._swap_b64 = swap_b64
        self._quote_error = quote_error
        self.quotes = 0
        self.swaps = 0
        self.last_fee = None

    def get_quote(self, input_mint, output_mint, amount,
                  slippage_bps=100):
        self.quotes += 1
        if self._quote_error is not None:
            raise self._quote_error
        q = dict(self._quote)
        q["inputMint"] = input_mint
        q["outputMint"] = output_mint
        q["inAmount"] = str(amount)
        return q

    def get_swap_transaction(self, quote, user_pubkey,
                             prioritization_fee_lamports=None):
        self.swaps += 1
        self.last_fee = prioritization_fee_lamports
        if self._swap_b64 is not None:
            return {"swapTransaction": self._swap_b64}
        return {"swapTransaction": _canned_swap_b64(
            self._kp, quote["inputMint"], quote["outputMint"])}


def _canned_swap_b64(kp, input_mint, output_mint,
                     program_id=None):
    """A v0 tx shaped like a Jupiter swap: one dummy instruction
    under program_id touching payer + both mints."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    program_id = program_id or tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID
    accounts = [AccountMeta(kp.pubkey(), True, True),
                AccountMeta(Pubkey.from_string(input_mint), False, True),
                AccountMeta(Pubkey.from_string(output_mint), False, True)]
    ix = Instruction(Pubkey.from_string(program_id), b"\x00\x01",
                     accounts)
    msg = MessageV0.try_compile(
        kp.pubkey(), [ix], [], Hash.from_string(_fake_blockhash()))
    vtx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(vtx)).decode("ascii")


class _FakeRPC:
    """Stateful fake RPC: balances flip after a send."""

    def __init__(self, sol_before=10_000_000, sol_after=None,
                 token_before=0, token_after=1_000_000,
                 token_ata=None, confirm=True, sim_err=None):
        self.sol_before = sol_before
        self.sol_after = sol_before if sol_after is None \
            else sol_after
        self.token_before = token_before
        self.token_after = token_after
        self.token_ata = token_ata
        self.confirm = confirm
        self.sim_err = sim_err
        self.sent = []
        self._sent_flag = False

    def get_health(self):
        return "ok"

    def get_slot(self):
        return 1

    def get_balance(self, pubkey):
        return self.sol_after if self._sent_flag else self.sol_before

    def get_token_account_balance(self, pubkey):
        if pubkey == self.token_ata:
            return {"amount": str(self.token_after
                                 if self._sent_flag
                                 else self.token_before),
                    "decimals": 6, "uiAmount": 0.0}
        raise BrokerError("rpc_error", "account not found",
                          detail={"reason": "rpc_error"})

    def simulate_transaction(self, b64_tx):
        return {"err": self.sim_err}

    def send_transaction(self, b64_tx):
        self.sent.append(b64_tx)
        self._sent_flag = True
        return _fake_sig(b64_tx)

    def get_signature_statuses(self, sigs):
        if not self.confirm:
            return [None for _ in sigs]
        return [{"confirmationStatus": "confirmed", "err": None}
                for _ in sigs]


def _fake_sig(b64_tx):
    # The executor compares the RPC echo to its local signature;
    # return the local one by re-deriving it from the tx bytes.
    vtx = tx_mod.deserialize_versioned_tx(b64_tx)
    return tx_mod.versioned_signature_str(vtx)


def _executor(tmp_home, kp, provider, rpc, jupiter, **kw):
    ex = MainnetExecutor(key_provider=provider, rpc=rpc,
                         jupiter=jupiter, live_home=tmp_home, **kw)
    marker = os.path.join(tmp_home, "live", "deployment.json")
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w") as f:
        json.dump({"machine": "test-vm", "armed": True}, f)
    ex.connect()
    return ex


def main():
    from solders.keypair import Keypair
    kp = Keypair()
    secret = json.dumps(list(bytes(kp.to_bytes())))
    provider = _StaticProvider(secret)
    pubkey = str(kp.pubkey())
    tok_mint = _b58encode(bytes(range(1, 33)))
    ata = associated_token_address(pubkey, tok_mint)
    stake = 1_000_000  # 0.001 SOL in lamports

    with tempfile.TemporaryDirectory() as tmp:
        # -- happy path buy -------------------------------------------
        rpc = _FakeRPC(sol_before=10_000_000,
                       sol_after=10_000_000 - stake - 10_000,
                       token_before=0, token_after=1_000_000,
                       token_ata=ata)
        jup = _FakeJupiter(kp)
        ex = _executor(tmp, kp, provider, rpc, jup)
        ex.arm_live()
        res = ex.execute_swap(SOL_MINT, tok_mint, stake)
        check("happy-path buy returns a signature",
              bool(res["signature"]))
        check("happy-path buy not simulated", res["simulated"] is False)
        check("happy-path buy out_amount from balances",
              res["out_amount"] == 1_000_000)
        check("happy-path buy fee measured from balances",
              res["priority_fee_lamports"] == 10_000)
        check("happy-path buy sent exactly once", len(rpc.sent) == 1)
        check("happy-path buy passed fee to Jupiter",
              jup.last_fee == 10_000)

        # -- dry-run never sends ---------------------------------------
        rpc2 = _FakeRPC(token_ata=ata)
        jup2 = _FakeJupiter(kp)
        ex2 = _executor(tmp, kp, provider, rpc2, jup2)
        check("dry-run default still True", ex2.dry_run is True)
        res2 = ex2.execute_swap(SOL_MINT, tok_mint, stake)
        check("dry-run returns simulated", res2["simulated"] is True)
        check("dry-run has no signature", res2["signature"] is None)
        check("dry-run never calls send", len(rpc2.sent) == 0)
        check("dry-run still simulates (swap built)",
              jup2.swaps == 1)

        # -- over-cap price impact is a skip ---------------------------
        jup3 = _FakeJupiter(
            kp, quote={"outAmount": "1000000",
                       "priceImpactPct": "5.0"})
        ex3 = _executor(tmp, kp, provider, _FakeRPC(token_ata=ata),
                        jup3)
        ex3.arm_live()
        try:
            ex3.execute_swap(SOL_MINT, tok_mint, stake)
            raise AssertionError("traded over price-impact cap")
        except BrokerError as e:
            check("over-cap impact is a skip",
                  (e.detail or {}).get("reason")
                  == "price_impact_over_cap")
        check("over-cap impact never quoted a swap",
              jup3.swaps == 0)

        # -- no route (400) is a skip ----------------------------------
        err400 = BrokerError("rpc_error", "jupiter quote failed",
                             detail={"reason": "jupiter_http_error",
                                     "http_status": 400,
                                     "body": "No route found"})
        jup4 = _FakeJupiter(kp, quote_error=err400)
        ex4 = _executor(tmp, kp, provider, _FakeRPC(token_ata=ata),
                        jup4)
        ex4.arm_live()
        try:
            ex4.execute_swap(SOL_MINT, tok_mint, stake)
            raise AssertionError("traded with no route")
        except BrokerError as e:
            check("no-route is a skip",
                  (e.detail or {}).get("reason") == "no_route")

        # -- fee over cap is a skip -------------------------------------
        jup5 = _FakeJupiter(kp)
        ex5 = _executor(tmp, kp, provider, _FakeRPC(token_ata=ata),
                        jup5)
        ex5.arm_live()
        try:
            ex5.execute_swap(SOL_MINT, tok_mint, stake,
                             priority_fee_lamports=100_000)
            raise AssertionError("paid fee over cap")
        except BrokerError as e:
            check("fee over cap is a skip",
                  (e.detail or {}).get("reason") == "fee_over_cap")
        check("fee over cap never built a swap", jup5.swaps == 0)

        # -- failed simulate never sends --------------------------------
        rpc6 = _FakeRPC(token_ata=ata, sim_err="InstructionError")
        ex6 = _executor(tmp, kp, provider, rpc6, _FakeJupiter(kp))
        ex6.arm_live()
        try:
            ex6.execute_swap(SOL_MINT, tok_mint, stake)
            raise AssertionError("sent after failed simulate")
        except BrokerError as e:
            check("failed simulate raises",
                  (e.detail or {}).get("reason") == "simulate_failed")
        check("failed simulate never sends", len(rpc6.sent) == 0)

        # -- confirm timeout: UNKNOWN, signature attached, no retry -----
        rpc7 = _FakeRPC(token_ata=ata, confirm=False)
        ex7 = _executor(tmp, kp, provider, rpc7, _FakeJupiter(kp))
        ex7.arm_live()
        try:
            ex7.execute_swap(SOL_MINT, tok_mint, stake,
                             confirm_timeout=0.1)
            raise AssertionError("returned on unconfirmed send")
        except BrokerError as e:
            det = e.detail or {}
            check("confirm timeout is UNKNOWN with signature",
                  det.get("reason") == "confirm_timeout"
                  and bool(det.get("signature")))
        check("unconfirmed send never retried",
              len(rpc7.sent) == 1)

        # -- sanity-check failure never sends ---------------------------
        bad_b64 = _canned_swap_b64(
            kp, SOL_MINT, tok_mint,
            program_id=_b58encode(bytes(range(3, 35))))
        jup8 = _FakeJupiter(kp, swap_b64=bad_b64)
        rpc8 = _FakeRPC(token_ata=ata)
        ex8 = _executor(tmp, kp, provider, rpc8, jup8)
        ex8.arm_live()
        try:
            ex8.execute_swap(SOL_MINT, tok_mint, stake)
            raise AssertionError("signed a bad-program tx")
        except BrokerError:
            pass
        check("bad-program swap tx never sends", len(rpc8.sent) == 0)

    print("ALL %d CHECKS PASSED" % _PASSED)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
