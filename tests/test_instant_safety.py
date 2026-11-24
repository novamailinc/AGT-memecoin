"""Tests for instant_safety: the on-chain fallback verdict used when
RugCheck has not indexed a token yet (2026-10-20).

RPC is mocked at the module's _rpc seam. Run: python3 tests/test_instant_safety.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "daemon", "launch_scanner"))
import instant_safety as ins  # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


WALLET = "Fn9n3ftUzjhcmoLwXbTqJdATzamCp3xPEu4LmLSS8ELr"  # on-curve
MINT = "AKuvHXF6H2ajMQhEPn3GpEq9FbDQixg2X27jHNbtpump"
CURVE = ins._pump_curve_pda(MINT)
SUPPLY = 1_000_000_000  # raw units


def make_rpc(mint_auth=None, freeze_auth=None, holders=(),
             extensions=None, owner=None):
    """holders: list of (token_account, raw_amount, owner)."""
    def _rpc(method, params, timeout):
        if method == "getAccountInfo" and params[0] == MINT:
            info = {
                "mintAuthority": mint_auth,
                "freezeAuthority": freeze_auth,
                "supply": str(SUPPLY), "decimals": 6}
            if extensions is not None:
                info["extensions"] = extensions
            return {"value": {
                "owner": owner or ins.TOKEN2022_PROGRAM,
                "data": {"parsed": {"info": info}}}}
        if method == "getTokenLargestAccounts":
            return {"value": [
                {"address": a, "amount": str(amt), "decimals": 6,
                 "uiAmount": amt / 1e6} for a, amt, _o in holders]}
        if method == "getMultipleAccounts":
            by_addr = {a: o for a, _amt, o in holders}
            return {"value": [
                {"data": {"parsed": {"info": {"owner": by_addr[a]}}}}
                if a in by_addr else None for a in params[0]]}
        return None
    return _rpc


def run(name, rpc, **kw):
    ins._cache.clear()
    orig = ins._rpc
    ins._rpc = rpc
    try:
        rep = ins.check(MINT, timeout=6)
    finally:
        ins._rpc = orig
    print("--- %s: passed=%s score=%s unknown=%s fails=%s flags=%s"
          % (name, rep.get("passed"), rep.get("score"),
             rep.get("unknown"), rep.get("fails"), rep.get("flags")))
    return rep


# Clean graduated token: authorities revoked, dispersed holders.
rep = run("clean", make_rpc(holders=[
    ("AcctA", 40_000_000, WALLET), ("AcctB", 20_000_000, WALLET)]))
check("clean passes", rep["passed"] and not rep["unknown"])
check("clean score 95 (LP flag only)", rep["score"] == 95)
check("clean top1 4%", rep["instant"]["top1_user_pct"] == 4.0)

ONE_HOLDER = [("AcctA", 40_000_000, WALLET)]

# Wallet-held mint authority -> hard fail.
rep = run("mint authority", make_rpc(mint_auth=WALLET,
                                     holders=ONE_HOLDER))
check("mint authority fails", not rep["passed"]
      and any("mint authority" in f for f in rep["fails"]))

# Curve-held mint authority -> flag, not fail (pre-graduation normal).
rep = run("curve authority", make_rpc(mint_auth=CURVE,
                                      holders=ONE_HOLDER))
check("curve authority passes with flag", rep["passed"]
      and any("pump.fun curve" in f for f in rep["flags"]))

# Freeze authority -> flag only (mirrors the RugCheck path).
rep = run("freeze authority", make_rpc(freeze_auth=WALLET,
                                       holders=ONE_HOLDER))
check("freeze flags not fails", rep["passed"]
      and any("freeze" in f for f in rep["flags"]))

# Whale: one user wallet at 60% -> fail. Program vault at 90% ignored.
vault_owner = ins._find_pda([b"vault"], ins.PUMP_PROGRAM)  # a PDA
rep = run("whale", make_rpc(holders=[
    ("WhaleAcct", 600_000_000, WALLET),
    ("VaultAcct", 900_000_000, vault_owner)]))
check("whale fails", not rep["passed"]
      and any("top holder owns 60%" in f for f in rep["fails"]))
check("vault excluded from user pct",
      rep["instant"]["program_held_pct"] == 90.0)

# Top-10 user concentration 75% -> fail.
many = [("H%d" % i, 75_000_000, WALLET) for i in range(10)]
rep = run("top10 concentration", make_rpc(holders=many))
check("top10 75% fails", not rep["passed"]
      and any("top 10 holders own 75%" in f for f in rep["fails"]))

# RPC dead -> unknown (fail-closed contract preserved).
rep = run("rpc down", lambda m, p, t: None)
check("rpc down stays unknown", rep["unknown"] is True)

# --- Token-2022 extension traps (2026-10-20) --------------------------
def ext(name, state=None):
    return {"extension": name, "state": state or {}}


# permanentDelegate -> FAIL.
rep = run("t22 permanent delegate", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("permanentDelegate", {"delegate": WALLET})]))
check("permanent delegate fails", not rep["passed"]
      and any("permanent delegate" in f for f in rep["fails"]))
check("permanent delegate score 70 (1 fail + LP flag)",
      rep["score"] == 70)

# transferHook -> FAIL.
rep = run("t22 transfer hook", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("transferHook", {"programId": WALLET})]))
check("transfer hook fails", not rep["passed"]
      and any("transfer hook" in f for f in rep["fails"]))

# nonTransferable -> FAIL.
rep = run("t22 non-transferable", make_rpc(
    holders=ONE_HOLDER, extensions=[ext("nonTransferable")]))
check("non-transferable fails", not rep["passed"]
      and any("non-transferable" in f for f in rep["fails"]))

# defaultAccountState frozen -> FAIL; initialized -> no effect.
rep = run("t22 default frozen", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("defaultAccountState", {"state": "frozen"})]))
check("default frozen fails", not rep["passed"]
      and any("frozen by default" in f for f in rep["fails"]))
rep = run("t22 default initialized", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("defaultAccountState", {"state": "initialized"})]))
check("default initialized passes clean (score 95)",
      rep["passed"] and rep["score"] == 95)

# transferFeeConfig: >10% FAIL (nested newerTransferFee spelling),
# 5% FLAG (top-level spelling), 0% silent.
rep = run("t22 fee 15pct", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("transferFeeConfig", {
        "newerTransferFee": {"transferFeeBasisPoints": 1500,
                             "maximumFee": "1000000",
                             "withheldFees": "0"}})]))
check("transfer fee 15% fails", not rep["passed"]
      and any("transfer fee 15.0%" in f for f in rep["fails"]))
rep = run("t22 fee 5pct", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("transferFeeConfig",
                    {"transferFeeBasisPoints": 500})]))
check("transfer fee 5% flags only", rep["passed"]
      and any("transfer fee 5.0%" in f for f in rep["flags"])
      and rep["score"] == 90)
rep = run("t22 fee 0", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("transferFeeConfig", {
        "olderTransferFee": {"transferFeeBasisPoints": 0}})]))
check("transfer fee 0% silent (score 95)",
      rep["passed"] and rep["score"] == 95)

# mintCloseAuthority set -> FLAG.
rep = run("t22 mint close authority", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("mintCloseAuthority",
                    {"mintCloseAuthority": WALLET})]))
check("mint close authority flags only", rep["passed"]
      and any("mint close authority" in f for f in rep["flags"]))

# confidentialTransferMint -> FLAG.
rep = run("t22 confidential transfer", make_rpc(
    holders=ONE_HOLDER,
    extensions=[ext("confidentialTransferMint",
                    {"autoApproveNewAccounts": True})]))
check("confidential transfer flags only", rep["passed"]
      and any("confidential transfer" in f for f in rep["flags"]))

# Classic SPL mint (Token program, no extensions): verdict unchanged.
rep = run("classic mint control", make_rpc(
    holders=ONE_HOLDER, owner=ins.TOKEN_PROGRAM))
check("classic mint passes clean (score 95)",
      rep["passed"] and rep["score"] == 95
      and rep["instant"]["token2022_extensions"] == [])

# Malformed extension data: flagged for REVIEW (Phase 2, §7.2) —
# never silently passed — while the parseable hook still fails.
# Score 65 = 1 fail + LP flag + malformed-entry flag.
rep = run("t22 malformed extensions", make_rpc(
    holders=ONE_HOLDER,
    extensions=["junk", {"state": {}}, {"extension": 7},
                {"extension": "transferHook", "state": "junk"},
                None]))
check("malformed entries flagged for review, hook still fails",
      not rep["passed"]
      and any("transfer hook" in f for f in rep["fails"])
      and any("entry unreadable" in f for f in rep["flags"])
      and rep["score"] == 65)
rep = run("t22 extensions not a list", make_rpc(
    holders=ONE_HOLDER, extensions="junk"))
check("non-list extensions flagged for review (score 90)",
      rep["passed"] and rep["score"] == 90
      and any("extensions data unreadable" in f for f in rep["flags"]))

# --- Pacer-aware _rpc (2026-11-17, pacing fix) ---
# _rpc skips Helius legs whose bucket is cooling down fleet-wide
# (no dead-key attempts), still tries mainnet-beta, takes a pacer
# token per Helius leg, reports a 429 (HTTP or JSON-RPC -32005) to
# the pacer (progressive backoff + single probe), and clears the
# cooldown on a success. The pacer mirrors outcomes into the
# shared guard file, so the hot-bucket skip keeps working off
# pacer state. Guard + pacer both live on a temp home per test —
# never the live state files (an earlier version of these tests
# polluted the live pacer/guard files with fake buckets).
import json as _json
import tempfile as _tempfile
import time as _time
import urllib.error as _urlerr
import urllib.request as _urlreq
from unittest.mock import patch as _patch

import helius_ratelimit as _rl


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._payload


def _rpc_ok(result):
    return _Resp(_json.dumps({"jsonrpc": "2.0", "id": 1,
                              "result": result}).encode())


def _rpc_err(code, message="Rate limit exceeded"):
    return _Resp(_json.dumps({"jsonrpc": "2.0", "id": 1,
                              "error": {"code": code,
                                        "message": message}}).encode())


_PAIRS = [("https://mainnet.helius-rpc.com/?api-key=K1", "gk1"),
          ("https://mainnet.helius-rpc.com/?api-key=K2", "gk2"),
          ("https://api.mainnet-beta.solana.com", None)]

# One temp home per test: the guard + pacer state files are shared
# by design, so tests must not share a home or one test's cooldown
# leaks into the next (and must never touch the live files).
_TMPS = []


def _mk_home():
    """Temp home for guard + pacer; singletons reset to it."""
    tmp = _tempfile.TemporaryDirectory()
    _TMPS.append(tmp)  # keep alive for the life of the test file
    os.environ["FOREX_AGENT_HOME"] = tmp.name
    _rl._PACER = None
    _rl._SHARED_GUARD = None
    return _rl.SharedGuard(home=tmp.name)


def _pacer_backoff(bucket):
    st = _rl.pacer()._read_state().get(bucket) or {}
    return st.get("backoff_s")


def _guard_file_until(bucket):
    """Shared-file cooldown for a bucket (the pacer mirror writes
    here; a patched guard instance's in-memory ladder is its own)."""
    path = os.path.join(os.environ["FOREX_AGENT_HOME"],
                        "helius_guard.json")
    with open(path) as fh:
        data = _json.load(fh)
    return (data.get("buckets") or {}).get(bucket, {}).get("until", 0.0)


def _expire_pacer_cooldown(bucket):
    """Set one pacer bucket's cooldown into the past."""
    home = os.environ["FOREX_AGENT_HOME"]
    path = os.path.join(home, "helius_pacer.json")
    with open(path) as fh:
        data = _json.load(fh)
    data["buckets"][bucket]["cooldown_until"] = _time.time() - 1.0
    with open(path, "w") as fh:
        _json.dump(data, fh)


# Guard hot for both keys: Helius legs skipped, public still tried.
_g = _mk_home()
_g.report_ratelimited("gk1")
_g.report_ratelimited("gk2")
_calls = []
with _patch.object(ins, "_rpc_endpoint_pairs", return_value=_PAIRS), \
        _patch.object(ins, "_shared_guard", return_value=_g):
    def _pub_only(req, timeout=None):
        _calls.append(req.full_url)
        return _rpc_ok({"value": "PUBLIC"})
    with _patch.object(_urlreq, "urlopen", side_effect=_pub_only):
        _r = ins._rpc("getAccountInfo", [MINT], 2)
check("rpc: hot guard skips Helius legs, public still tried",
      _r == {"value": "PUBLIC"} and len(_calls) == 1
      and "mainnet-beta" in _calls[0])

# HTTP 429 on a Helius leg engages that key's pacer backoff (mirrored
# into the shared guard); the public leg still serves the call.
_g = _mk_home()
_calls = []
with _patch.object(ins, "_rpc_endpoint_pairs", return_value=_PAIRS), \
        _patch.object(ins, "_shared_guard", return_value=_g):
    def _k1_429(req, timeout=None):
        _calls.append(req.full_url)
        if "api-key=K1" in req.full_url:
            raise _urlerr.HTTPError(req.full_url, 429,
                                    "Too Many Requests", {}, None)
        return _rpc_ok({"value": "PUBLIC"})
    with _patch.object(_urlreq, "urlopen", side_effect=_k1_429):
        _r = ins._rpc("getAccountInfo", [MINT], 2)
check("rpc: 429 leg skipped to public, result still served",
      _r == {"value": "PUBLIC"} and len(_calls) == 2
      and "api-key=K1" in _calls[0])
check("rpc: HTTP 429 engages that key's pacer backoff",
      _g.cooldown_remaining("gk1") > 20.0
      and _g.cooldown_remaining("gk2") == 0.0
      and _pacer_backoff("gk1") == 30.0)

# Success on a Helius leg clears that key's fleet cooldown — once
# the cooldown has expired (a hot leg is skipped, so the success can
# only land after expiry) — and resets the pacer backoff ladder.
_g = _mk_home()
_rl.pacer_report("gk1", False)  # pacer backoff 30s
_expire_pacer_cooldown("gk1")
_rl.pacer_report("gk1", False)  # probe 429 doubles it to 60s
_g.report_ratelimited("gk1")  # guard file hot too (60s ladder)
_t0 = _time.time()
with _patch.object(ins, "_rpc_endpoint_pairs", return_value=_PAIRS), \
        _patch.object(ins, "_shared_guard", return_value=_g), \
        _patch("time.time", return_value=_t0 + 61):
    def _k1_ok(req, timeout=None):
        return _rpc_ok({"value": "H1"})
    with _patch.object(_urlreq, "urlopen", side_effect=_k1_ok):
        _r = ins._rpc("getAccountInfo", [MINT], 2)
check("rpc: Helius success returns the result",
      _r == {"value": "H1"})
check("rpc: Helius success after expiry clears the cooldown",
      _guard_file_until("gk1") == 0.0)
check("rpc: Helius success resets the pacer backoff ladder",
      _pacer_backoff("gk1") == 30.0)

# JSON-RPC rate-limit error (-32005, HTTP 200) engages the pacer.
_g = _mk_home()
_calls = []
with _patch.object(ins, "_rpc_endpoint_pairs", return_value=_PAIRS), \
        _patch.object(ins, "_shared_guard", return_value=_g):
    def _k1_json429(req, timeout=None):
        _calls.append(req.full_url)
        if "api-key=K1" in req.full_url:
            return _rpc_err(-32005)
        return _rpc_ok({"value": "PUBLIC"})
    with _patch.object(_urlreq, "urlopen", side_effect=_k1_json429):
        _r = ins._rpc("getAccountInfo", [MINT], 2)
check("rpc: JSON-RPC -32005 still serves via public",
      _r == {"value": "PUBLIC"})
check("rpc: JSON-RPC -32005 engages that key's pacer backoff",
      _g.cooldown_remaining("gk1") > 20.0
      and _pacer_backoff("gk1") == 30.0)

print("\n%d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
