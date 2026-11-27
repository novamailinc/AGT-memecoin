#!/usr/bin/env python3
"""Phase 2 tests (plain script — run with the repo venv python,
never pytest):

    ~/workspace/Forex-agent/.venv/bin/python tests/test_phase2_execution.py

Covers, with zero network and ephemeral in-memory keypairs only
(never written to disk): fee-policy tiers + both caps + the
uneconomic-$1-tip verdict with exact math; signed-tx-template
build/sign/expiry/invalidation/param-mismatch refusal; Jito payload
construction (JSON-RPC shape, tip tx present) + mocked send +
status polling + dry-run-never-posts; executor dry-run with
use_bundle=True (simulated + bundle payload, zero HTTP posts);
executor without Jito configured behaving exactly as before.

Phase 1 is UNPROVEN (one post-fix close, Helius quota spent, zero
new entries). Nothing here claims otherwise.
"""

import base64
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from broker import BrokerError
from broker.memecoin.execution import tx as tx_mod
from broker.memecoin.execution.fee_policy import (
    PriorityFeePolicy, jito_tip_verdict)
from broker.memecoin.execution.jito import (
    DEFAULT_BLOCK_ENGINE_URL, JitoClient, TIP_ACCOUNTS)
from broker.memecoin.execution.jupiter import (
    DEFAULT_JUPITER_BASE, JupiterClient)
from broker.memecoin.execution.mainnet_executor import (
    MainnetExecutor, SOL_MINT, associated_token_address)
from broker.memecoin.execution.tx_templates import SignedTxTemplate


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


def _canned_swap_b64(kp, input_mint, output_mint):
    """A v0 tx shaped like a Jupiter swap: one dummy instruction
    under the Jupiter program id touching payer + both mints."""
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    accounts = [AccountMeta(kp.pubkey(), True, True),
                AccountMeta(Pubkey.from_string(input_mint), False, True),
                AccountMeta(Pubkey.from_string(output_mint), False, True)]
    ix = Instruction(
        Pubkey.from_string(tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID),
        b"\x00\x01", accounts)
    msg = MessageV0.try_compile(
        kp.pubkey(), [ix], [], Hash.from_string(_fake_blockhash()))
    vtx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(vtx)).decode("ascii")


# Address lookup table fixture: the table key every ALT test uses,
# and the real on-chain ALT meta (56 bytes, captured from mainnet
# table 6rqx7NGgRAtGiAsBE6RAxWMaNQfSgzRy1tMSNML4GzQm on
# 2026-11-20). A hand-built zero meta does NOT parse (solders
# raises UninitializedAccount / InvalidAccountData), so the blob
# is the real meta + synthetic addresses.
_ALT_TABLE_KEY = _b58encode(bytes(range(5, 37)))
_ALT_META_HEX = (
    "01000000ffffffffffffffffa3c4e11400000000"
    "58017d0ea3424053053bbbd860f74c32f04bc3dc"
    "872adf9a4f4ed170c24ee0f43ed30000")
_ALT_PROGRAM_ID = \
    "AddressLookupTab1e1111111111111111111111111"


def _alt_account_blob(addrs):
    """Base64 ALT account data: the real meta + synthetic
    addresses (32 bytes each, no length prefix)."""
    from solders.pubkey import Pubkey
    raw = bytes.fromhex(_ALT_META_HEX) + b"".join(
        bytes(Pubkey.from_string(a)) for a in addrs)
    return base64.b64encode(raw).decode("ascii")


def _canned_alt_swap_b64(kp, input_mint, output_mint):
    """A v0 tx shaped like a real Jupiter swap: the mints live
    ONLY in an address lookup table (static keys = payer +
    program). The table also holds one extra address the tx never
    references."""
    from solders.address_lookup_table_account import \
        AddressLookupTableAccount
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.transaction import VersionedTransaction
    extra = _b58encode(bytes(range(9, 41)))
    table = AddressLookupTableAccount(
        key=Pubkey.from_string(_ALT_TABLE_KEY),
        addresses=[Pubkey.from_string(a) for a in
                   (input_mint, output_mint, extra)])
    accounts = [AccountMeta(kp.pubkey(), True, True),
                AccountMeta(Pubkey.from_string(input_mint), False, True),
                AccountMeta(Pubkey.from_string(output_mint), False, True)]
    ix = Instruction(
        Pubkey.from_string(tx_mod.JUPITER_AGGREGATOR_PROGRAM_ID),
        b"\x00\x01", accounts)
    msg = MessageV0.try_compile(
        kp.pubkey(), [ix], [table],
        Hash.from_string(_fake_blockhash()))
    vtx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(vtx)).decode("ascii")


def _alt_resolver(input_mint, output_mint):
    """A resolver for _canned_alt_swap_b64's table: returns the
    table's addresses, None for any other table."""
    extra = _b58encode(bytes(range(9, 41)))

    def resolve(table):
        if table == _ALT_TABLE_KEY:
            return [input_mint, output_mint, extra]
        return None
    return resolve


class _StaticProvider:
    def __init__(self, secret):
        self._secret = secret

    def get_key(self, chain_name):
        return self._secret

    def describe_source(self, chain_name):
        return "test-static"


class _FakeJupiter:
    def __init__(self, kp, quote=None, swap_b64=None):
        self._kp = kp
        self._quote = quote or {
            "outAmount": "1000000", "priceImpactPct": "0.005"}
        # swap_b64 overrides the canned tx (e.g. an ALT-loaded tx).
        self._swap_b64 = swap_b64
        self.quotes = 0
        self.swaps = 0
        self.last_fee = None

    def get_quote(self, input_mint, output_mint, amount,
                  slippage_bps=100):
        self.quotes += 1
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


class _FakeRPC:
    """Stateful fake RPC: balances flip once a send lands."""

    def __init__(self, sol_before=10_000_000_000,
                 sol_after=None, token_before=0,
                 token_after=1_000_000, token_ata=None,
                 alt_tables=None):
        self.sol_before = sol_before
        self.sol_after = sol_before if sol_after is None else sol_after
        self.token_before = token_before
        self.token_after = token_after
        self.token_ata = token_ata
        # alt_tables: {table_key: base64 account blob} served by
        # call("getAccountInfo", ...) for the ALT resolver.
        self.alt_tables = alt_tables or {}
        self.sent = []
        self.landed = False

    def call(self, method, params):
        if method == "getAccountInfo":
            blob = self.alt_tables.get(params[0])
            if blob is None:
                return {"context": {"slot": 1}, "value": None}
            return {"context": {"slot": 1},
                    "value": {"owner": _ALT_PROGRAM_ID,
                              "data": [blob, "base64"]}}
        raise BrokerError("rpc_error", "unsupported method",
                          detail={"reason": "rpc_error"})

    def get_health(self):
        return "ok"

    def get_slot(self):
        return 123456

    def get_balance(self, pubkey):
        return self.sol_after if self.landed else self.sol_before

    def get_token_account_balance(self, pubkey):
        if pubkey == self.token_ata:
            return {"amount": str(self.token_after if self.landed
                                 else self.token_before),
                    "decimals": 6, "uiAmount": 0.0}
        raise BrokerError("rpc_error", "account not found",
                          detail={"reason": "rpc_error"})

    def simulate_transaction(self, b64_tx):
        return {"err": None}

    def send_transaction(self, b64_tx):
        self.sent.append(b64_tx)
        self.landed = True
        vtx = tx_mod.deserialize_versioned_tx(b64_tx)
        return tx_mod.versioned_signature_str(vtx)

    def get_signature_statuses(self, sigs):
        return [{"confirmationStatus": "confirmed", "err": None}
                for _ in sigs]


def _executor(tmp_home, provider, rpc, jupiter, **kw):
    ex = MainnetExecutor(key_provider=provider, rpc=rpc,
                         jupiter=jupiter, live_home=tmp_home, **kw)
    marker = os.path.join(tmp_home, "live", "deployment.json")
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w") as f:
        json.dump({"machine": "test-vm", "armed": True}, f)
    ex.connect()
    return ex


class TestFeePolicy(unittest.TestCase):
    def test_calm_tier_low_fees(self):
        p = PriorityFeePolicy(fee_source=lambda: [5000, 8000, 9000])
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["verdict"], "ok")
        self.assertEqual(d["tier"], "calm")
        self.assertEqual(d["fee_lamports"], 10_000)  # base-fee floor

    def test_normal_tier(self):
        p = PriorityFeePolicy(fee_source=lambda: [50000] * 5)
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["tier"], "normal")
        self.assertEqual(d["fee_lamports"], 50000)

    def test_hot_tier(self):
        p = PriorityFeePolicy(fee_source=lambda: [200000] * 10)
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["tier"], "hot")
        self.assertEqual(d["fee_lamports"], 200000)

    def test_absolute_cap_is_a_skip(self):
        p = PriorityFeePolicy(fee_source=lambda: [2_000_000] * 5)
        d = p.choose_fee(100_000_000)
        self.assertEqual(d["verdict"], "skip")
        self.assertEqual(d["reason"], "fee_over_cap")

    def test_frac_cap_is_a_skip(self):
        p = PriorityFeePolicy(fee_source=lambda: [100_000] * 5)
        d = p.choose_fee(1_000_000)  # 5% of stake is 50k < 100k fee
        self.assertEqual(d["verdict"], "skip")
        self.assertEqual(d["reason"], "fee_over_cap")

    def test_empty_source_falls_back_to_base_fee(self):
        p = PriorityFeePolicy(fee_source=lambda: [])
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["verdict"], "ok")
        self.assertEqual(d["fee_lamports"], 10_000)
        self.assertEqual(d["tier"], "calm")
        self.assertEqual(d["samples"], 0)

    def test_rpc_rows_parsed(self):
        rows = [{"slot": 1, "prioritizationFee": 7000},
                {"slot": 2, "prioritizationFee": 9000},
                {"slot": 3, "prioritizationFee": "bad"}]
        p = PriorityFeePolicy(fee_source=lambda: rows)
        self.assertEqual(p.sample_fees(), [7000, 9000])

    def test_rpc_backed_source(self):
        class _FakeRPCCall:
            def __init__(self):
                self.calls = []

            def call(self, method, params):
                self.calls.append((method, params))
                return [{"slot": 9, "prioritizationFee": 12000}]

        rpc = _FakeRPCCall()
        p = PriorityFeePolicy(rpc=rpc)
        d = p.choose_fee(10_000_000)
        self.assertEqual(d["fee_lamports"], 12000)
        self.assertEqual(d["tier"], "normal")
        self.assertEqual(rpc.calls[0][0], "getRecentPrioritizationFees")

    def test_dollar_stake_tip_uneconomic_exact_math(self):
        # $1 stake ~= 0.01 SOL at $100/SOL; 0.001 SOL tip = 10%.
        v = jito_tip_verdict(0.01, 0.001, sol_price_usd=100.0)
        self.assertEqual(v["verdict"], "uneconomic")
        self.assertAlmostEqual(v["tip_frac"], 0.10)
        self.assertEqual(v["tip_sol"], 0.001)
        self.assertEqual(v["stake_sol"], 0.01)
        self.assertEqual(v["tip_lamports"], 1_000_000)
        self.assertEqual(v["stake_lamports"], 10_000_000)
        self.assertAlmostEqual(v["tip_usd"], 0.10)
        self.assertAlmostEqual(v["stake_usd"], 1.00)

    def test_biggest_documented_tip_eats_dollar_stake(self):
        v = jito_tip_verdict(0.01, 0.01)
        self.assertEqual(v["verdict"], "uneconomic")
        self.assertAlmostEqual(v["tip_frac"], 1.0)

    def test_big_stake_tip_economic(self):
        v = jito_tip_verdict(1.0, 0.001)
        self.assertEqual(v["verdict"], "economic")
        self.assertAlmostEqual(v["tip_frac"], 0.001)

    def test_zero_tip_economic(self):
        self.assertEqual(jito_tip_verdict(0.01, 0.0)["verdict"],
                         "economic")

    def test_policy_check_jito_tip_reason(self):
        p = PriorityFeePolicy(fee_source=lambda: [])
        v = p.check_jito_tip(0.01, 0.001)
        self.assertEqual(v["verdict"], "uneconomic")
        self.assertEqual(v["reason"], "fee_uneconomic")
        self.assertIn("shakedown", v["detail"])


class TestTxTemplate(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.kp = Keypair()
        self.wallet = str(self.kp.pubkey())
        self.tok = _b58encode(bytes(range(1, 33)))
        self.b64 = _canned_swap_b64(self.kp, SOL_MINT, self.tok)

    def _build(self, **kw):
        kw.setdefault("created_at", 1000.0)
        return SignedTxTemplate.build(
            self.b64, self.kp, self.wallet, SOL_MINT, self.tok,
            1_000_000, 100, slot=100, **kw)

    def test_build_signs_once_and_reuses(self):
        # Real creation time here: the signed_base64 property checks
        # the live clock, so a template built "in the past" (as the
        # expiry tests do) is correctly refused.
        t = SignedTxTemplate.build(
            self.b64, self.kp, self.wallet, SOL_MINT, self.tok,
            1_000_000, 100, slot=100)
        self.assertTrue(t.signed_base64)
        self.assertTrue(t.signature)
        self.assertTrue(t.blockhash)
        self.assertEqual(
            t.reuse_for(self.wallet, SOL_MINT, self.tok, 1_000_000,
                        100, current_slot=101),
            t.signed_base64)

    def test_no_key_material_stored(self):
        t = self._build()
        keys = " ".join(vars(t).keys()).lower()
        self.assertNotIn("keypair", keys)
        self.assertNotIn("secret", keys)
        self.assertNotIn("seed", keys)

    def test_param_mismatch_refuses(self):
        t = self._build()
        for args in (
                ("other-wallet", SOL_MINT, self.tok, 1_000_000, 100),
                (self.wallet, self.tok, SOL_MINT, 1_000_000, 100),
                (self.wallet, SOL_MINT, self.tok, 999_999, 100),
                (self.wallet, SOL_MINT, self.tok, 1_000_000, 200)):
            with self.assertRaises(BrokerError) as ctx:
                t.reuse_for(*args, now=1001.0)
            self.assertEqual(
                (ctx.exception.detail or {}).get("reason"),
                "template_param_mismatch")

    def test_time_expiry(self):
        t = self._build(ttl_seconds=60)
        self.assertFalse(t.is_expired(now=1059.0))
        self.assertTrue(t.is_expired(now=1061.0))
        with self.assertRaises(BrokerError) as ctx:
            t.reuse_for(self.wallet, SOL_MINT, self.tok, 1_000_000,
                        100, now=1061.0)
        self.assertEqual(
            (ctx.exception.detail or {}).get("reason"),
            "template_expired")

    def test_slot_expiry(self):
        t = self._build(ttl_slots=150)
        self.assertFalse(t.is_expired(now=1001.0, current_slot=250))
        self.assertTrue(t.is_expired(now=1001.0, current_slot=251))
        # A slot that moved backwards is fail-closed: expired.
        self.assertTrue(t.is_expired(now=1001.0, current_slot=99))

    def test_invalidate_kills_it(self):
        t = self._build()
        t.invalidate()
        self.assertTrue(t.is_expired(now=1001.0))
        with self.assertRaises(BrokerError) as ctx:
            _ = t.signed_base64
        self.assertEqual(
            (ctx.exception.detail or {}).get("reason"),
            "template_expired")

    def test_bad_swap_tx_refuses_build(self):
        bad = _canned_swap_b64(self.kp, SOL_MINT, self.tok)
        # Corrupt the program id by rebuilding against a random
        # program: use a different canned tx whose payer is not us.
        from solders.keypair import Keypair
        other = Keypair()
        other_b64 = _canned_swap_b64(other, SOL_MINT, self.tok)
        with self.assertRaises(BrokerError):
            SignedTxTemplate.build(other_b64, self.kp, self.wallet,
                                   SOL_MINT, self.tok, 1_000_000, 100)
        _ = bad  # (kept for clarity: build validates before signing)

    def test_build_with_alt_loaded_mints(self):
        # Bundle path: the template builder must accept a swap tx
        # whose mints live only in an ALT, given a resolver.
        alt_b64 = _canned_alt_swap_b64(self.kp, SOL_MINT, self.tok)
        t = SignedTxTemplate.build(
            alt_b64, self.kp, self.wallet, SOL_MINT, self.tok,
            1_000_000, 100, slot=100,
            lookup_table_resolver=_alt_resolver(SOL_MINT, self.tok))
        self.assertTrue(t.signed_base64)
        self.assertTrue(t.signature)

    def test_build_with_alt_loaded_mints_refuses_without_resolver(self):
        # Fail-closed: an ALT tx with no resolver is never signed.
        alt_b64 = _canned_alt_swap_b64(self.kp, SOL_MINT, self.tok)
        with self.assertRaises(BrokerError):
            SignedTxTemplate.build(
                alt_b64, self.kp, self.wallet, SOL_MINT, self.tok,
                1_000_000, 100, slot=100)


class TestJitoClient(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.kp = Keypair()
        self.wallet = str(self.kp.pubkey())

    def test_default_endpoint(self):
        c = JitoClient()
        self.assertEqual(c.base_url, DEFAULT_BLOCK_ENGINE_URL)

    def test_payload_shape(self):
        c = JitoClient()
        payload = c.build_bundle_payload(["aaa", "bbb"], 1_000_000)
        self.assertEqual(payload["jsonrpc"], "2.0")
        self.assertEqual(payload["method"], "sendBundle")
        self.assertEqual(payload["params"], [["aaa", "bbb"]])

    def test_payload_validation(self):
        c = JitoClient()
        for txs, tip in (([], 1_000_000), (["a"] * 6, 1_000_000),
                         (["a"], 0), ([""], 1_000_000)):
            with self.assertRaises(BrokerError):
                c.build_bundle_payload(txs, tip)

    def test_tip_tx_is_a_transfer_to_a_tip_account(self):
        from solders.transaction import Transaction
        c = JitoClient()
        unsigned = c.build_tip_tx(self.wallet, 1_000_000,
                                  _fake_blockhash())
        self.assertFalse(unsigned.is_signed())
        from broker.memecoin.execution import signer as signer_mod
        signer_mod.DevnetSigner.sign_transaction(
            unsigned, self.kp, _fake_blockhash())
        b64 = tx_mod.tx_to_base64(unsigned)
        tx = Transaction.from_bytes(base64.b64decode(b64))
        keys = [str(k) for k in tx.message.account_keys]
        self.assertEqual(keys[0], self.wallet)
        self.assertIn(keys[1], TIP_ACCOUNTS)
        data = bytes(tx.message.instructions[0].data)
        self.assertEqual(struct.unpack("<I", data[:4])[0], 2)  # transfer
        self.assertEqual(struct.unpack("<Q", data[4:12])[0], 1_000_000)

    def test_tip_account_round_robin(self):
        c = JitoClient()
        picks = [c.pick_tip_account() for _ in range(9)]
        self.assertEqual(sorted(picks[:8]), sorted(TIP_ACCOUNTS))
        self.assertEqual(picks[8], picks[0])

    def test_mocked_send_and_status_polling(self):
        calls = []

        def _post(url, payload):
            calls.append((url, payload))
            if payload["method"] == "sendBundle":
                return {"jsonrpc": "2.0", "id": payload["id"],
                        "result": "bundle-uuid-1"}
            return {"jsonrpc": "2.0", "id": payload["id"],
                    "result": [{"bundle_id": "bundle-uuid-1",
                                "confirmation_status": "confirmed"}]}

        c = JitoClient(http_post=_post)
        res = c.send_bundle(["aaa", "bbb"], 1_000_000)
        self.assertEqual(res["bundle_id"], "bundle-uuid-1")
        self.assertFalse(res["dry_run"])
        self.assertTrue(calls[0][0].endswith("/api/v1/bundles"))
        self.assertEqual(calls[0][1]["method"], "sendBundle")
        statuses = c.get_bundle_statuses(["bundle-uuid-1"])
        self.assertEqual(statuses[0]["confirmation_status"], "confirmed")
        self.assertEqual(calls[1][1]["method"], "getBundleStatuses")
        self.assertEqual(calls[1][1]["params"], [["bundle-uuid-1"]])

    def test_get_tip_accounts_refreshes(self):
        def _post(url, payload):
            return {"jsonrpc": "2.0", "id": 1,
                    "result": ["Tip111111111111111111111111111111111111111",
                               "Tip222222222222222222222222222222222222222"]}
        c = JitoClient(http_post=_post)
        got = c.get_tip_accounts()
        self.assertEqual(len(got), 2)
        self.assertEqual(c.tip_accounts, got)

    def test_dry_run_never_posts(self):
        def _post(url, payload):
            raise AssertionError("dry-run client must never POST")

        c = JitoClient(http_post=_post, dry_run=True)
        res = c.send_bundle(["aaa", "bbb"], 1_000_000)
        self.assertTrue(res["dry_run"])
        self.assertIsNone(res["bundle_id"])
        self.assertEqual(res["payload"]["method"], "sendBundle")
        self.assertEqual(res["payload"]["params"], [["aaa", "bbb"]])
        with self.assertRaises(BrokerError) as ctx:
            c.get_bundle_statuses(["bundle-uuid-1"])
        self.assertEqual(
            (ctx.exception.detail or {}).get("reason"),
            "jito_dry_run_no_poll")


class TestJupiterClient(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.kp = Keypair()
        self.wallet = str(self.kp.pubkey())
        self.tok = _b58encode(bytes(range(1, 33)))

    def test_default_base_is_reachable_v1_host(self):
        # The old v6 host (quote-api.jup.ag) is unreachable from
        # this VM (HTTP 000) and /v6/ 404s on the new host;
        # api.jup.ag/swap/v1 answered HTTP 200 live on 2026-11-20.
        # The client must point at the host that was verified
        # reachable.
        c = JupiterClient()
        self.assertEqual(c._base, "https://api.jup.ag")
        self.assertEqual(DEFAULT_JUPITER_BASE, "https://api.jup.ag")

    def test_quote_and_swap_hit_swap_v1_paths(self):
        import broker.memecoin.execution.jupiter as jup_mod
        seen = {}
        orig_get = jup_mod._get_json
        orig_post = jup_mod._post_json

        def _fake_get(url, params, timeout):
            seen["get"] = url
            return {"outAmount": "1"}

        def _fake_post(url, payload, timeout):
            seen["post"] = url
            return {"swapTransaction": "AAAA"}

        try:
            jup_mod._get_json = _fake_get
            jup_mod._post_json = _fake_post
            c = JupiterClient()
            c.get_quote(SOL_MINT, self.tok, 1_000_000)
            c.get_swap_transaction({"outAmount": "1"}, self.wallet)
        finally:
            jup_mod._get_json = orig_get
            jup_mod._post_json = orig_post
        self.assertEqual(seen["get"],
                         "https://api.jup.ag/swap/v1/quote")
        self.assertEqual(seen["post"],
                         "https://api.jup.ag/swap/v1/swap")


class TestExecutorBundle(unittest.TestCase):
    def setUp(self):
        from solders.keypair import Keypair
        self.kp = Keypair()
        secret = json.dumps(list(bytes(self.kp.to_bytes())))
        self.provider = _StaticProvider(secret)
        self.pubkey = str(self.kp.pubkey())
        self.tok = _b58encode(bytes(range(1, 33)))
        self.ata = associated_token_address(self.pubkey, self.tok)
        self.tmp = tempfile.TemporaryDirectory()
        self.stake_1sol = 1_000_000_000
        # DISCLOSED fixture (2026-11-24): arming/sending now requires a registry label AND owner admission; neither exists for these
        # fake-RPC/Jupiter armed-path tests, so the label goes to a throwaway home and ONLY the owner-admission step is replaced (scoped, restored).
        import tests._guard_fixture as _GF
        _GF.admit_registry_for_fixture()
        _p = _GF.stub_owner_admission()
        self.addCleanup(_p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _policy(self, fees=(5000, 8000, 9000)):
        return PriorityFeePolicy(fee_source=lambda: list(fees))

    def _jito(self, dry_run=False, post=None):
        return JitoClient(http_post=post, dry_run=dry_run)

    def test_dry_run_bundle_returns_payload_and_posts_nothing(self):
        posts = []

        def _post(url, payload):
            posts.append((url, payload))
            return {"result": "bundle-never"}

        rpc = _FakeRPC(token_ata=self.ata)
        jup = _FakeJupiter(self.kp)
        ex = _executor(self.tmp.name, self.provider, rpc, jup,
                       fee_policy=self._policy(),
                       jito=self._jito(dry_run=True, post=_post))
        res = ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol,
                              use_bundle=True)
        self.assertTrue(res["simulated"])
        self.assertIsNone(res["signature"])
        self.assertEqual(res["fee_tier"], "calm")
        self.assertEqual(res["jito_tip_lamports"], 1_000_000)
        self.assertIsNone(res["bundle_id"])
        payload = res["bundle"]["payload"]
        self.assertEqual(payload["method"], "sendBundle")
        txs = payload["params"][0]
        self.assertEqual(len(txs), 2)  # swap tx + tip tx
        # Second tx is a signed tip transfer for 1_000_000 lamports.
        from solders.transaction import Transaction
        tip_tx = Transaction.from_bytes(base64.b64decode(txs[1]))
        keys = [str(k) for k in tip_tx.message.account_keys]
        self.assertEqual(keys[0], self.pubkey)
        self.assertIn(keys[1], TIP_ACCOUNTS)
        data = bytes(tip_tx.message.instructions[0].data)
        self.assertEqual(struct.unpack("<Q", data[4:12])[0], 1_000_000)
        self.assertEqual(posts, [])  # zero HTTP posts
        self.assertEqual(rpc.sent, [])  # zero direct sends
        self.assertEqual(jup.swaps, 1)

    def test_live_bundle_sends_via_jito_not_rpc(self):
        rpc = _FakeRPC(sol_before=10_000_000_000,
                       sol_after=10_000_000_000 - self.stake_1sol
                       - 10_000,
                       token_before=0, token_after=1_000_000,
                       token_ata=self.ata)
        posts = []

        def _post(url, payload):
            posts.append((url, payload))
            rpc.landed = True  # bundle landing flips balances
            return {"jsonrpc": "2.0", "id": payload["id"],
                    "result": "bundle-live-1"}

        jup = _FakeJupiter(self.kp)
        ex = _executor(self.tmp.name, self.provider, rpc, jup,
                       fee_policy=self._policy(),
                       jito=self._jito(post=_post))
        ex.arm_live()
        res = ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol,
                              use_bundle=True)
        self.assertFalse(res["simulated"])
        self.assertTrue(res["signature"])
        self.assertEqual(res["bundle_id"], "bundle-live-1")
        self.assertEqual(res["out_amount"], 1_000_000)
        self.assertEqual(res["jito_tip_lamports"], 1_000_000)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]["method"], "sendBundle")
        self.assertEqual(rpc.sent, [])  # never rpc.send_transaction

    def test_executor_without_jito_behaves_as_before(self):
        rpc = _FakeRPC(sol_before=10_000_000_000,
                       sol_after=10_000_000_000 - self.stake_1sol
                       - 10_000,
                       token_before=0, token_after=1_000_000,
                       token_ata=self.ata)
        jup = _FakeJupiter(self.kp)
        ex = _executor(self.tmp.name, self.provider, rpc, jup)
        # Dry-run: same shape as the old path, no bundle keys.
        res = ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol)
        self.assertTrue(res["simulated"])
        self.assertIsNone(res["signature"])
        self.assertNotIn("bundle", res)
        self.assertNotIn("bundle_id", res)
        self.assertEqual(res["priority_fee_lamports"], 10_000)
        self.assertEqual(rpc.sent, [])
        # Live: same happy path as before, direct RPC send.
        ex.arm_live()
        res2 = ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol)
        self.assertFalse(res2["simulated"])
        self.assertTrue(res2["signature"])
        self.assertEqual(res2["out_amount"], 1_000_000)
        self.assertEqual(res2["priority_fee_lamports"], 10_000)
        self.assertEqual(len(rpc.sent), 1)
        self.assertEqual(jup.last_fee, 10_000)

    def test_dry_run_with_alt_loaded_mints(self):
        # Non-bundle path: a swap tx whose mints live only in an
        # ALT passes the check via the executor's RPC resolver.
        alt_b64 = _canned_alt_swap_b64(self.kp, SOL_MINT, self.tok)
        blob = _alt_account_blob(
            [SOL_MINT, self.tok, _b58encode(bytes(range(9, 41)))])
        rpc = _FakeRPC(token_ata=self.ata,
                       alt_tables={_ALT_TABLE_KEY: blob})
        jup = _FakeJupiter(self.kp, swap_b64=alt_b64)
        ex = _executor(self.tmp.name, self.provider, rpc, jup)
        res = ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol)
        self.assertTrue(res["simulated"])
        self.assertIsNone(res["signature"])
        self.assertEqual(jup.swaps, 1)

    def test_dry_run_bundle_with_alt_loaded_mints(self):
        # Bundle path: the template build inside execute_swap gets
        # the same RPC resolver.
        alt_b64 = _canned_alt_swap_b64(self.kp, SOL_MINT, self.tok)
        blob = _alt_account_blob(
            [SOL_MINT, self.tok, _b58encode(bytes(range(9, 41)))])
        rpc = _FakeRPC(token_ata=self.ata,
                       alt_tables={_ALT_TABLE_KEY: blob})
        jup = _FakeJupiter(self.kp, swap_b64=alt_b64)
        ex = _executor(self.tmp.name, self.provider, rpc, jup,
                       fee_policy=self._policy(),
                       jito=self._jito(dry_run=True))
        res = ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol,
                              use_bundle=True)
        self.assertTrue(res["simulated"])
        self.assertIsNone(res["bundle_id"])
        self.assertEqual(len(res["bundle"]["payload"]["params"][0]), 2)

    def test_alt_tx_with_unresolvable_table_refuses(self):
        # Fail-closed at the executor level: the ALT table is not
        # served by the RPC, so the swap is refused before signing.
        alt_b64 = _canned_alt_swap_b64(self.kp, SOL_MINT, self.tok)
        rpc = _FakeRPC(token_ata=self.ata)
        jup = _FakeJupiter(self.kp, swap_b64=alt_b64)
        ex = _executor(self.tmp.name, self.provider, rpc, jup)
        with self.assertRaises(BrokerError):
            ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol)

    def test_use_bundle_without_jito_refuses(self):
        ex = _executor(self.tmp.name, self.provider,
                       _FakeRPC(token_ata=self.ata), _FakeJupiter(self.kp),
                       fee_policy=self._policy())
        with self.assertRaises(BrokerError) as ctx:
            ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol,
                            use_bundle=True)
        self.assertEqual(
            (ctx.exception.detail or {}).get("reason"),
            "jito_not_configured")

    def test_use_bundle_without_fee_policy_refuses(self):
        ex = _executor(self.tmp.name, self.provider,
                       _FakeRPC(token_ata=self.ata), _FakeJupiter(self.kp),
                       jito=self._jito(dry_run=True))
        with self.assertRaises(BrokerError) as ctx:
            ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol,
                            use_bundle=True)
        self.assertEqual(
            (ctx.exception.detail or {}).get("reason"),
            "fee_policy_required")

    def test_dollar_stake_bundle_skips_uneconomic(self):
        # $1 stake = 10_000_000 lamports at $100/SOL; the default
        # 0.001 SOL tip is 10% of it -> fee_uneconomic, no swap built.
        rpc = _FakeRPC(token_ata=self.ata)
        jup = _FakeJupiter(self.kp)
        ex = _executor(self.tmp.name, self.provider, rpc, jup,
                       fee_policy=self._policy(),
                       jito=self._jito(dry_run=True))
        with self.assertRaises(BrokerError) as ctx:
            ex.execute_swap(SOL_MINT, self.tok, 10_000_000,
                            use_bundle=True)
        det = ctx.exception.detail or {}
        self.assertEqual(det.get("reason"), "fee_uneconomic")
        self.assertAlmostEqual(det.get("tip_frac"), 0.10)
        self.assertEqual(jup.swaps, 0)
        self.assertEqual(rpc.sent, [])

    def test_policy_fee_over_cap_skips_bundle(self):
        rpc = _FakeRPC(token_ata=self.ata)
        jup = _FakeJupiter(self.kp)
        ex = _executor(self.tmp.name, self.provider, rpc, jup,
                       fee_policy=self._policy(fees=(2_000_000,) * 5),
                       jito=self._jito(dry_run=True))
        with self.assertRaises(BrokerError) as ctx:
            ex.execute_swap(SOL_MINT, self.tok, self.stake_1sol,
                            use_bundle=True)
        self.assertEqual(
            (ctx.exception.detail or {}).get("reason"), "fee_over_cap")
        self.assertEqual(jup.swaps, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
