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

# Actual central router, no Helius fallback even with a configured old key.
import json as _json
import tempfile as _tempfile
import urllib.error as _urlerr
from daemon.intelligence.acquisition import routine as _routine
from daemon.intelligence.acquisition.provider_router import ProviderRouter as _Router
class _Resp:
    def __init__(self,result): self.result=result
    def read(self): return _json.dumps({"jsonrpc":"2.0","id":1,"result":self.result}).encode()
    def __enter__(self): return self
    def __exit__(self,*a): return False
_calls=[]
def _transport(req,timeout=None):
    _calls.append(req.full_url.split('?')[0])
    if 'alchemy.com' in req.full_url:
        raise _urlerr.URLError('fixture outage')
    return _Resp({"value":"FALLBACK"})
os.environ['ALCHEMY_API_KEY']='fixture-not-real'
os.environ['HELIUS_API_KEY']='fixture-not-real'
os.environ.pop('CHAINSTACK_ENDPOINT',None)
_router=_Router(home=_tempfile.mkdtemp(),helius_enabled=False,transport=_transport)
_previous=getattr(_routine._local,'router',None)
_routine._local.router=_router
_routine.clear_evidence()
try:
    _result=ins._rpc("getAccountInfo", [MINT], 2)
    check("central router serves fallback account read",_result=={"value":"FALLBACK"})
    check("Alchemy primary, no Helius request",len(_calls)==2 and 'alchemy.com' in _calls[0] and all('helius' not in x for x in _calls))
    _ev=_routine.evidence_since()[-1]
    check("fallback retains degraded consumer evidence",_ev['provider']=='public' and _ev['degraded'] and _ev['decision_usable'])
finally:
    if _previous is None: del _routine._local.router
    else: _routine._local.router=_previous

print("\n%d failures" % len(FAILS))
sys.exit(1 if FAILS else 0)
