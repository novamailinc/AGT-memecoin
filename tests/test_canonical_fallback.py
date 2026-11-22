#!/usr/bin/env python3
"""Canonical-pool chain fallback tests (2026-11-20).

Covers grad_select.py Filter 3's backfill fallback: when the
recorder has no migration record (PumpPortal stream down), a
GeckoTerminal-backfill candidate's pool is resolved from the
chain. The migration tx is found by scanning the curve's
newest successful signatures around the pool's creation second
(the candidate's created_ts, from GeckoTerminal's
pool_created_at, ±2s — GT lags the migration by ~1s) for the
one whose logs say "Instruction: Migrate". A single pick is
not reliable: the newest successful signature is often a
post-migration SweepProtocolFee tx (verified live 2026-11-20
on FomoCoin), and an exact-second match misses real
migrations (GT's 1s lag, verified on D1keaV2J1HmR). A
non-migration pick is skipped, never a verdict; the scan is
bounded to 3 tx fetches (4 RPC calls max). The pool is
canonical iff it is one of the migration tx's account keys.
Mock reserves.rpc — no network. Plain script — run directly,
not via pytest.
"""
import os
import sys
import tempfile
import time

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import grad_select as GS
from daemon.launch_scanner import saturation as S

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


NOW = time.time()
CREATED = int(NOW)  # pool creation second (candidate created_ts)
MINT_A = "FbMintA11111111111111111111111111111111pump"
MINT_B = "FbMintB11111111111111111111111111111111pump"
MINT_C = "FbMintC11111111111111111111111111111111pump"
MINT_D = "FbMintD11111111111111111111111111111111pump"
MINT_E = "FbMintE11111111111111111111111111111111pump"
TRADER_OK = "FbTraderOK1111111111111111111111111111111"
POOL_REAL = "PoolReal11111111111111111111111111111111"
POOL_OTHER = "PoolOther1111111111111111111111111111111"
CURVE_FB = "CurveFB1111111111111111111111111111111111"

eng = GS.make_engine()
_tmp = tempfile.mkdtemp(prefix="canonfb-ledger-")
eng.LEDGER_PATH = os.path.join(_tmp, "grad_select_ledger.json")
eng.ensure_ledger()

# ---------------- filter data stubs ----------------
GS._index_refresh = lambda now: None
GS._INDEX["available"] = True
GS._INDEX["coverage_start"] = NOW - 90000

for mint in (MINT_A, MINT_B, MINT_C, MINT_D, MINT_E):
    GS._INDEX["by_mint"][mint] = {
        "trader": TRADER_OK, "ts": NOW - 600,
        "bonding_curve": CURVE_FB}
GS._INDEX["by_trader"][TRADER_OK] = [NOW - 600]

GS._curve_stats = lambda addr: {
    "trades": 80, "created_ts": NOW - 600, "oldest_sig": "sig-fb",
    "capped": False, "exhausted": True, "creation_slot": 100,
    "rpc_source": "test", "cache": "miss"}
GS._creator_from_oldest_tx = lambda sig: None


class _FakeSat:
    def __init__(self):
        self.launches = {}
        self.updated = NOW

    def _refresh_if_stale(self):
        return {"updated": self.updated, "wm_ts": NOW * 1000,
                "launches": dict(self.launches)}

    def _norm(self, s):
        return str(s or "").strip().upper().lstrip("$").strip()

    def rolling_count(self, state, symbol, window_s=86400, now_s=None):
        return S.rolling_count(state, symbol, window_s, now_s)


GS._saturation = _FakeSat()


class _FakeReserves:
    """Records every RPC call; returns canned curve sigs + tx."""

    def __init__(self):
        self.calls = []
        self.sigs = {}
        self.txs = {}

    def rpc(self, method, params, timeout=12, exempt=False):
        self.calls.append((method, params))
        if method == "getSignaturesForAddress":
            return self.sigs.get(params[0])
        if method == "getTransaction":
            return self.txs.get(params[0])
        return None

    def derive_curve_pda(self, mint):
        return CURVE_FB


class _FakeCache:
    """Records curve-cache puts; never returns a hit."""

    def __init__(self):
        self.puts = []

    def curve_cache(self):
        return self

    def put(self, addr, count, oldest):
        self.puts.append((addr, count, oldest))

    def get(self, addr):
        return None


_FAKE_RPC = _FakeReserves()
_FAKE_CACHE = _FakeCache()
GS._reserves = _FAKE_RPC
GS._curve_cache = _FAKE_CACHE

# Curve signatures, newest first. At the pool's creation second
# (blockTime == CREATED) there are three: a FAILED trade (highest
# slot, skipped), the MIGRATION (highest-slot successful — the
# pick), and a successful TRADE (same second, lower slot — not
# the pick). An older successful trade and the create sig sit in
# earlier seconds.
_MIG_SIGS = [
    {"signature": "sig-failed-trade", "slot": 301,
     "blockTime": CREATED, "err": {"InstructionError": [0, "Custom"]}},
    {"signature": "sig-mig", "slot": 299,
     "blockTime": CREATED, "err": None},
    {"signature": "sig-trade", "slot": 298,
     "blockTime": CREATED, "err": None},
    {"signature": "sig-old", "slot": 100,
     "blockTime": CREATED - 600, "err": None},
    {"signature": "sig-create", "slot": 50,
     "blockTime": CREATED - 600, "err": None},
]
_MIG_TX = {
    "slot": 299,
    "meta": {"err": None,
             "logMessages": [
                 "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
                 "Program log: Instruction: Migrate",
                 "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P success"]},
    "transaction": {"message": {"accountKeys": [
        {"pubkey": TRADER_OK, "signer": True, "writable": True},
        {"pubkey": POOL_REAL, "signer": False, "writable": True},
        CURVE_FB,  # plain-string entry: also handled
        {"pubkey": "MintX1111111111111111111111111111111111",
         "signer": False, "writable": False},
    ]}},
    "blockTime": CREATED,
}


def _reset_rpc():
    _FAKE_RPC.calls = []
    _FAKE_RPC.sigs = {CURVE_FB: [dict(s) for s in _MIG_SIGS]}
    _FAKE_RPC.txs = {"sig-mig": _MIG_TX}
    _FAKE_CACHE.puts = []


def _backfill_cand(mint, pool, source="gecko-new-pool"):
    return {"mint": mint, "symbol": "FBONE", "created_ts": NOW,
            "dex": "pumpswap", "pool_address": pool,
            "reserve_usd": 8000.0, "fdv_usd": 70000.0,
            "source": source}


def _stream_cand(mint):
    return {"mint": mint, "symbol": "FBONE", "created_ts": NOW,
            "dex": "pumpswap", "pool_address": POOL_REAL,
            "migration_sig": "", "reserve_usd": 0.0, "fdv_usd": 0.0,
            "source": "pumpportal-migration"}


# ---------------- (a) pool matches the migration tx: PASS ------------
# The live source is "gecko-new-pool" (GT new_pools rows carry it;
# the scan's setdefault("geckoterminal-backfill") does not override).
_reset_rpc()
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_A, POOL_REAL),
                          NOW)
check("(a) backfill pool in migration tx PASSES (gecko-new-pool)",
      v["verdict"] == "pass"
      and v["values"].get("canonical_basis")
      == "chain_migration_match")
check("(a) exactly 2 RPC calls: 1 sigs + 1 tx, paced path",
      len(_FAKE_RPC.calls) == 2
      and _FAKE_RPC.calls[0][0] == "getSignaturesForAddress"
      and _FAKE_RPC.calls[0][1][0] == CURVE_FB
      and _FAKE_RPC.calls[1][0] == "getTransaction"
      and _FAKE_RPC.calls[1][1][0] == "sig-mig")
check("(a) failed trade skipped, lower-slot trade not picked",
      _FAKE_RPC.calls[1][1][0] == "sig-mig")
check("(a) raw curve data shared with the curve cache",
      len(_FAKE_CACHE.puts) == 1
      and _FAKE_CACHE.puts[0][0] == CURVE_FB
      and _FAKE_CACHE.puts[0][1] == 5
      and (_FAKE_CACHE.puts[0][2] or {}).get("signature")
      == "sig-create")

# The documented backfill source also runs the fallback.
_reset_rpc()
v = GS.evaluate_candidate(
    eng, _backfill_cand(MINT_A, POOL_REAL,
                        source="geckoterminal-backfill"), NOW)
check("(a2) geckoterminal-backfill source also runs the fallback",
      v["verdict"] == "pass"
      and v["values"].get("canonical_basis")
      == "chain_migration_match"
      and len(_FAKE_RPC.calls) == 2)

# (a3) A SweepProtocolFee tx sits at a HIGHER slot than the
# migration in the creation window: the scan skips it (not a
# Migrate) and finds the migration behind it — 3 calls.
_reset_rpc()
_sweep_sigs = [dict(s) for s in _MIG_SIGS]
_sweep_sigs.insert(0, {"signature": "sig-sweep", "slot": 305,
                       "blockTime": CREATED, "err": None})
_sweep_tx = {"slot": 305,
             "meta": {"err": None,
                      "logMessages": [
                          "Program log: Instruction: SweepProtocolFee"]},
             "transaction": {"message": {"accountKeys": [
                 {"pubkey": TRADER_OK, "signer": True,
                  "writable": True},
                 CURVE_FB]}},
             "blockTime": CREATED}
_FAKE_RPC.sigs = {CURVE_FB: _sweep_sigs}
_FAKE_RPC.txs = {"sig-mig": _MIG_TX, "sig-sweep": _sweep_tx}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_A, POOL_REAL),
                          NOW)
check("(a3) sweep skipped, migration found behind it: PASSES",
      v["verdict"] == "pass"
      and v["values"].get("canonical_basis")
      == "chain_migration_match")
check("(a3) 3 RPC calls: 1 sigs + sweep tx + mig tx",
      len(_FAKE_RPC.calls) == 3
      and _FAKE_RPC.calls[1][1][0] == "sig-sweep"
      and _FAKE_RPC.calls[2][1][0] == "sig-mig")

# ---------------- (b) pool NOT in the migration tx -------------------
_reset_rpc()
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_B, POOL_OTHER),
                          NOW)
check("(b) backfill pool absent from migration tx REJECTED",
      v["verdict"] == "reject"
      and v["reason"] == "noncanonical_pool"
      and v["values"].get("canonical_basis")
      == "chain_migration_mismatch")
check("(b) the migration tx was still fetched (2 calls)",
      len(_FAKE_RPC.calls) == 2)

# ---------------- (c) unresolvable: fail closed -----------------------
_reset_rpc()
_FAKE_RPC.sigs = {CURVE_FB: None}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_C, POOL_REAL),
                          NOW)
check("(c1) signatures call fails -> unknown_canonical",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical")
check("(c1) no tx call spent after a failed sigs call",
      len(_FAKE_RPC.calls) == 1)

_reset_rpc()
_FAKE_RPC.txs = {"sig-mig": None}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_C, POOL_REAL),
                          NOW)
check("(c2) migration tx unfetchable -> unknown_canonical",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical")

_reset_rpc()
_FAKE_RPC.sigs = {CURVE_FB: []}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_C, POOL_REAL),
                          NOW)
check("(c3) empty signatures list -> unknown_canonical",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical")

_reset_rpc()
_bad_tx = dict(_MIG_TX)
_bad_tx["meta"] = {"err": {"InstructionError": [0, "Custom"]},
                   "logMessages": _MIG_TX["meta"]["logMessages"]}
_FAKE_RPC.txs = {"sig-mig": _bad_tx}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_C, POOL_REAL),
                          NOW)
check("(c4) reverted migration tx -> unknown_canonical",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical")

_reset_rpc()
_no_success = [dict(s) for s in _MIG_SIGS]
for s in _no_success:
    if s.get("blockTime") == CREATED:
        s["err"] = {"InstructionError": [0, "Custom"]}
_FAKE_RPC.sigs = {CURVE_FB: _no_success}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_C, POOL_REAL),
                          NOW)
check("(c5) no successful signature at creation second -> unknown",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical")

# (c6) No Migrate among the first 3 candidates in the window:
# the scan exhausts its tries and fails closed (unknown, never
# non-canonical — the migration may be a 4th sig). The migration
# sig is present at a lower slot but beyond the 3-try bound.
_reset_rpc()
_nomig_sigs = [
    {"signature": "sig-sweep1", "slot": 305,
     "blockTime": CREATED, "err": None},
    {"signature": "sig-sweep2", "slot": 304,
     "blockTime": CREATED, "err": None},
    {"signature": "sig-trade9", "slot": 303,
     "blockTime": CREATED, "err": None},
] + [dict(s) for s in _MIG_SIGS]
_nomig_tx = {"slot": 305,
             "meta": {"err": None,
                      "logMessages": [
                          "Program log: Instruction: SweepProtocolFee"]},
             "transaction": {"message": {"accountKeys": [
                 {"pubkey": TRADER_OK, "signer": True,
                  "writable": True},
                 CURVE_FB]}},
             "blockTime": CREATED}
_FAKE_RPC.sigs = {CURVE_FB: _nomig_sigs}
_FAKE_RPC.txs = {"sig-mig": _MIG_TX,
                 "sig-sweep1": _nomig_tx,
                 "sig-sweep2": _nomig_tx,
                 "sig-trade9": _nomig_tx}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_C, POOL_REAL),
                          NOW)
check("(c6) no Migrate in 3 tries -> unknown_canonical",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical"
      and v["values"].get("canonical_basis")
      == "chain_unresolvable")
check("(c6) bounded: 1 sigs + 3 tx calls, then stops",
      len(_FAKE_RPC.calls) == 4
      and _FAKE_RPC.calls[1][1][0] == "sig-sweep1"
      and _FAKE_RPC.calls[3][1][0] == "sig-trade9")

# (c7) No created_ts on the candidate: the fallback cannot find
# the migration second, so it never runs (fail closed, no RPC).
_reset_rpc()
noage = _backfill_cand(MINT_C, POOL_REAL)
del noage["created_ts"]
v = GS.evaluate_candidate(eng, noage, NOW)
check("(c7) missing created_ts -> unknown_canonical, zero RPC",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical"
      and len(_FAKE_RPC.calls) == 0)

# ---------------- (d) stream candidates pass free, no RPC ------------
_reset_rpc()
v = GS.evaluate_candidate(eng, _stream_cand(MINT_D), NOW)
check("(d) stream candidate PASSES free (migration_event)",
      v["verdict"] == "pass"
      and v["values"].get("canonical_basis") == "migration_event")
check("(d) stream candidate spends ZERO RPC calls",
      len(_FAKE_RPC.calls) == 0)

# ---------------- (e) recorder-hit path unchanged ---------------------
_reset_rpc()
GS._INDEX["by_mig"][MINT_E] = {"pool": POOL_REAL, "ts": NOW - 60}
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_E, POOL_REAL),
                          NOW)
check("(e1) recorder pool match PASSES without RPC",
      v["verdict"] == "pass"
      and v["values"].get("canonical_basis")
      == "recorder_migration_match"
      and len(_FAKE_RPC.calls) == 0)
v = GS.evaluate_candidate(eng, _backfill_cand(MINT_E, POOL_OTHER),
                          NOW)
check("(e2) recorder pool mismatch REJECTED without RPC",
      v["verdict"] == "reject"
      and v["reason"] == "noncanonical_pool"
      and v["values"].get("canonical_basis")
      == "recorder_migration_mismatch"
      and len(_FAKE_RPC.calls) == 0)
del GS._INDEX["by_mig"][MINT_E]

# ---------------- fallback is backfill-only ---------------------------
_reset_rpc()
other = _backfill_cand(MINT_A, POOL_REAL)
other["source"] = "pumpportal-new"
v = GS.evaluate_candidate(eng, other, NOW)
check("non-backfill source with a pool: no RPC, fail closed",
      v["verdict"] == "reject"
      and v["reason"] == "unknown_canonical"
      and len(_FAKE_RPC.calls) == 0)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
