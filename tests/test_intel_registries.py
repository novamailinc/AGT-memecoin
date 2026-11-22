#!/usr/bin/env python3
"""Intel registries tests (2026-10-23). Plain script — run with the
venv python, not pytest.

Covers daemon/launch_scanner/funding_clusters.py (union-find
clustering, resolver fail-soft on bad input + injected fake
transport, persistence) and rugger_registry.py (the >=3 dead flag
threshold on counter evidence AND on mint-level dead_mints joined
through dossiers, window-expired exclusion), plus dossier
surfacing fail-soft in dossiers.py. No live network: the resolver
is only ever called with fake transports.
"""
import json
import os
import sys
import tempfile

# Isolate FOREX_AGENT_HOME BEFORE importing the modules under test.
_TMP_HOME = tempfile.mkdtemp(prefix="intel-home-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import funding_clusters as FC
from daemon.launch_scanner import rugger_registry as RR
from daemon.launch_scanner import dossiers as D

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


FC.REGISTRY_PATH = os.path.join(_TMP_HOME, "funding_clusters.json")
RR.REGISTRY_PATH = os.path.join(_TMP_HOME, "rugger_registry.json")

# Plausible base58 wallets: 44 chars from the base58 alphabet
# (which excludes 0, O, I, and lowercase l).
def _mk(seed):
    return (seed * 44)[:44]


F = _mk("Fz")
G = _mk("Gz")
A = _mk("Aa")
B = _mk("Bb")
C = _mk("Cc")
D1 = _mk("Dd")
E = _mk("Ee")
H = _mk("Hh")
I = _mk("Jj")
for _w in (F, G, A, B, C, D1, E, H, I):
    assert FC.is_plausible_wallet(_w), _w

# ---------------- union-find clustering ----------------
check("funding: missing registry stats zero, no raise",
      FC.stats()["wallets_tracked"] == 0 and FC.cluster_of(A) is None)

FC.record_funder(A, F)
FC.record_funder(B, F)
FC.record_funder(C, F)
info = FC.cluster_of(A)
check("funding: shared funder -> cluster size 3",
      info is not None and info["size"] == 3)
check("funding: cluster reports the shared funder",
      info is not None and info["funder"] == F)
check("funding: members_sample covers the cluster",
      info is not None and set(info["members_sample"]) == {A, B, C})
check("funding: same cluster_id for all members",
      FC.cluster_of(B)["cluster_id"] == info["cluster_id"]
      and FC.cluster_of(C)["cluster_id"] == info["cluster_id"])

FC.record_funder(D1, G)
check("funding: singleton (one wallet per funder) is not a cluster",
      FC.cluster_of(D1) is None)
FC.record_funder(E, G)
check("funding: second wallet on funder G forms its own cluster",
      FC.cluster_of(E) is not None and FC.cluster_of(E)["size"] == 2
      and FC.cluster_of(E)["cluster_id"] != info["cluster_id"])

FC.record_funder(I, B)   # I funded by B, B funded by F -> chain joins
check("funding: funding chain joins transitively (union-find)",
      FC.cluster_of(I) is not None and FC.cluster_of(I)["size"] == 4
      and FC.cluster_of(I)["cluster_id"] == info["cluster_id"])

FC.record_funder(H, None)   # attempted, unknown
check("funding: unknown funder forms no cluster",
      FC.cluster_of(H) is None and FC.known_funder(H)["funder"] is None)

both = FC.clusters_for_wallets([A, D1, H, "nonexistent"])
check("funding: clusters_for_wallets returns clustered subset only",
      set(both.keys()) == {A, D1})

st = FC.stats()
check("funding: stats count tracked/resolved/clusters",
      st["wallets_tracked"] == 7 and st["wallets_resolved"] == 6
      and st["clusters"] == 2 and st["clustered_wallets"] == 6)

check("funding: cluster survives a fresh registry read (persisted)",
      FC.cluster_of(A)["size"] == 4)
check("funding: record_funder rejects garbage wallet, no raise",
      FC.record_funder("not a wallet!!", F) is None
      and FC.cluster_of("not a wallet!!") is None)

# corrupt registry -> reads as empty, never raises
with open(FC.REGISTRY_PATH, "w") as f:
    f.write("{corrupt")
check("funding: corrupt registry reads empty, no raise",
      FC.cluster_of(A) is None and FC.stats()["wallets_tracked"] == 0)
os.remove(FC.REGISTRY_PATH)
FC.record_funder(A, F)
FC.record_funder(B, F)

# ---------------- resolver: fail-soft + fake transport ----------------

def _boom(method, params):
    raise RuntimeError("net down")


def _none(method, params):
    return None


check("resolver: transport raising -> None (fail-soft)",
      FC.resolve_first_funder(A, rpc=_boom) is None)
check("resolver: transport returning None -> None",
      FC.resolve_first_funder(A, rpc=_none) is None)
check("resolver: empty/garbage wallet -> None without transport",
      FC.resolve_first_funder("", rpc=_boom) is None
      and FC.resolve_first_funder("!!!", rpc=_boom) is None
      and FC.resolve_first_funder(None, rpc=_boom) is None)

W_NEW = _mk("Nn")
FUNDER = _mk("Pp")
assert FC.is_plausible_wallet(W_NEW) and FC.is_plausible_wallet(FUNDER)
_TX = {"transaction": {"message": {
    "accountKeys": [{"pubkey": W_NEW, "signer": True}],
    "instructions": [{"parsed": {"type": "transfer", "info": {
        "source": FUNDER, "destination": W_NEW, "lamports": 1000000}}}]}},
    "meta": {"innerInstructions": []}}


def _fake_rpc(method, params):
    if method == "getSignaturesForAddress":
        return [{"signature": "sig-oldest"}, {"signature": "sig-newer"}][::-1]
    if method == "getTransaction":
        return _TX
    return None


check("resolver: fake transport resolves the funding transfer source",
      FC.resolve_first_funder(W_NEW, rpc=_fake_rpc) == FUNDER)
check("resolver: parse_funder_from_tx reads transfer directly",
      FC.parse_funder_from_tx(_TX, W_NEW) == FUNDER)
check("resolver: parse handles garbage tx -> None",
      FC.parse_funder_from_tx(None, W_NEW) is None
      and FC.parse_funder_from_tx({}, W_NEW) is None
      and FC.parse_funder_from_tx(_TX, "") is None)

_TX_PAYER = {"transaction": {"message": {
    "accountKeys": [{"pubkey": FUNDER}, {"pubkey": W_NEW}],
    "instructions": []}}, "meta": {}}
check("resolver: fee-payer fallback when no transfer present",
      FC.parse_funder_from_tx(_TX_PAYER, W_NEW) == FUNDER)
_TX_SELF = {"transaction": {"message": {
    "accountKeys": [{"pubkey": W_NEW}], "instructions": []}}, "meta": {}}
check("resolver: self-funded tx (payer == wallet) -> None",
      FC.parse_funder_from_tx(_TX_SELF, W_NEW) is None)


def _deep_rpc(method, params):
    if method == "getSignaturesForAddress":
        return [{"signature": "s%d" % i} for i in range(100)]
    return _TX


check("resolver: history deeper than page bound -> None (no guessing)",
      FC.resolve_first_funder(W_NEW, rpc=_deep_rpc, max_pages=2) is None)

# ---------------- rugger registry: threshold on counters ----------------
HOME2 = tempfile.mkdtemp(prefix="intel-rugger-")
RR.REGISTRY_PATH = os.path.join(HOME2, "rugger_registry.json")
os.makedirs(os.path.join(HOME2, "launch_scanner"), exist_ok=True)
os.makedirs(os.path.join(HOME2, "dossiers"), exist_ok=True)

DEP2 = _mk("Qq")
DEP3 = _mk("Rr")
DEP0 = _mk("Ss")
for _w in (DEP2, DEP3, DEP0):
    assert FC.is_plausible_wallet(_w), _w
hist = {
    DEP2: {"launches_seen": 5, "rugs": 1, "dead": 1, "hits_2x": 0},
    DEP3: {"launches_seen": 6, "rugs": 1, "dead": 2, "hits_2x": 0},
    DEP0: {"launches_seen": 4, "rugs": 0, "dead": 0, "hits_2x": 3},
}
with open(os.path.join(HOME2, "launch_scanner",
                       "deployer_history.json"), "w") as f:
    json.dump(hist, f)
RR.refresh()
check("rugger: 2 dead (1 rug + 1 dead) is NOT flagged",
      RR.lookup(DEP2) == {"launches": 5, "dead": 2, "flagged": False,
                          "examples": []})
r3 = RR.lookup(DEP3)
check("rugger: 3 dead (1 rug + 2 dead) IS flagged",
      r3["flagged"] is True and r3["dead"] == 3 and r3["launches"] == 6)
check("rugger: clean deployer zero-flagged",
      RR.lookup(DEP0)["flagged"] is False
      and RR.lookup(DEP0)["dead"] == 0)
check("rugger: unknown deployer -> zero record, no raise",
      RR.lookup("whatever") == {"launches": 0, "dead": 0,
                                "flagged": False, "examples": []})
check("rugger: stats count deployers + flagged",
      RR.stats() == {"deployers": 3, "flagged": 1})

# ---------------- rugger: mint-level dead_mints via dossiers ----------------
DEPM = _mk("Tt")
DEPW = _mk("Uu")
assert FC.is_plausible_wallet(DEPM) and FC.is_plausible_wallet(DEPW)
mints_m = [_mk("Vv"), _mk("Ww"), _mk("Xx")]
mints_w = [_mk("Yy"), _mk("Zz"), _mk("2a")]
for _m in mints_m + mints_w:
    assert FC.is_plausible_wallet(_m), _m
dead_mints = {}
for m in mints_m:
    dead_mints[m] = {"ts": 1.0, "reason": "safety: rugcheck danger: rug"}
for m in mints_w:
    dead_mints[m] = {"ts": 1.0, "reason": "window expired (312s old)"}
with open(os.path.join(HOME2, "grad_ledger.json"), "w") as f:
    json.dump({"dead_mints": dead_mints}, f)
for dep, mints in ((DEPM, mints_m), (DEPW, mints_w)):
    for m in mints:
        with open(os.path.join(HOME2, "dossiers", m + ".json"), "w") as f:
            json.dump({"mint": m, "deployer": dep, "born_ts": 1.0}, f)
RR.refresh()
rm = RR.lookup(DEPM)
check("rugger: 3 dossier-joined dead_mints flag the deployer",
      rm["flagged"] is True and rm["dead"] == 3
      and set(rm["examples"]) == set(mints_m))
check("rugger: 'window expired' dead_mints are NOT evidence",
      RR.lookup(DEPW)["flagged"] is False
      and RR.lookup(DEPW)["dead"] == 0)

# ---------------- dossier surfacing ----------------
D.DOSSIER_DIR = os.path.join(HOME2, "dossiers")
D._res = None
D._safety = None
check("dossier: _intel_fields empty when registries lack the deployer",
      D._intel_fields(_mk("3b")) == {})

fields = D._intel_fields(DEPM)
check("dossier: rugger summary surfaced for flagged deployer",
      fields.get("deployer_rugger") == {"flagged": True, "launches": 3,
                                        "dead": 3})

# Cluster the deployer with A+B's cluster funder, then re-check.
FC.record_funder(DEPM, F)
fields2 = D._intel_fields(DEPM)
check("dossier: funding cluster surfaced (id + size) for deployer",
      fields2.get("funding_cluster", {}).get("size") == 3
      and fields2["funding_cluster"]["cluster_id"]
      == FC.cluster_of(DEPM)["cluster_id"])

# Full _build with registries ABSENT must still produce a dossier.
FC.REGISTRY_PATH = os.path.join(HOME2, "absent_funding.json")
RR.REGISTRY_PATH = os.path.join(HOME2, "absent_rugger.json")
MINT_NEW = _mk("4c")
assert FC.is_plausible_wallet(MINT_NEW)
D._build({"mint": MINT_NEW, "symbol": "FRESH", "created_ts": 1.0,
          "extra": {"trader": DEP0, "initial_buy_sol": 2.0}})
built = None
try:
    with open(os.path.join(D.DOSSIER_DIR, MINT_NEW + ".json")) as f:
        built = json.load(f)
except Exception:
    built = None
check("dossier: _build succeeds with registries absent (fail-soft)",
      built is not None and built.get("deployer") == DEP0)
check("dossier: absent registries -> intel fields omitted, not fatal",
      built is not None and "deployer_rugger" not in built
      and "funding_cluster" not in built)


# ---------------- resolve queue: fresh-dossier-first ----------------
import importlib.machinery
import importlib.util

_loader = importlib.machinery.SourceFileLoader(
    "intel_refresh",
    os.path.join(REPO, "scripts", "intel-registries-refresh"))
_spec = importlib.util.spec_from_loader("intel_refresh", _loader)
IR = importlib.util.module_from_spec(_spec)
_loader.exec_module(IR)

W_OLD_DOSS = _mk("5d")
W_NEW_DOSS = _mk("6e")
W_SMART = _mk("7f")
W_UNKNOWN = _mk("8g")
for _w in (W_OLD_DOSS, W_NEW_DOSS, W_SMART, W_UNKNOWN):
    assert FC.is_plausible_wallet(_w), _w
_todo = [
    (W_UNKNOWN, {"first_seen": 0.0, "dossier_ts": None}),
    (W_SMART, {"first_seen": 99999.0, "dossier_ts": None}),
    (W_OLD_DOSS, {"first_seen": 100.0, "dossier_ts": 100.0}),
    (W_NEW_DOSS, {"first_seen": 50.0, "dossier_ts": 200.0}),
]
_ordered = [w for w, _i in IR.order_resolve_queue(_todo)]
check("queue: fresh dossier wallets first, newest sighting first",
      _ordered[:2] == [W_NEW_DOSS, W_OLD_DOSS])
check("queue: non-dossier wallets follow, newest first-seen first",
      _ordered[2] == W_SMART)
check("queue: unknown-ts (0) wallets go LAST, not first",
      _ordered[3] == W_UNKNOWN)

os.makedirs(os.path.join(_TMP_HOME, "dossiers"), exist_ok=True)
DW = _mk("9h")
assert FC.is_plausible_wallet(DW)
with open(os.path.join(_TMP_HOME, "dossiers", "somemint.json"), "w") as f:
    json.dump({"mint": _mk("2b"), "deployer": DW, "born_ts": 1234.0}, f)
_cands = IR.collect_candidates()
check("queue: dossier sighting stamps dossier_ts + first_seen",
      _cands.get(DW, {}).get("dossier_ts") == 1234.0
      and _cands.get(DW, {}).get("first_seen") == 1234.0)
check("queue: collect_wallets back-compat (wallet -> first_seen)",
      IR.collect_wallets().get(DW) == 1234.0)


print()
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("all intel registry tests passed")
