#!/usr/bin/env python3
"""Dossier build tests (Lever 3, 2026-11-16). Plain script — run with
the venv python, not pytest.

Covers daemon/launch_scanner/dossiers.py: the unused
curve_price_usd fetch is gone (one curve get_account per build),
the cross-process per-mint flock dedups concurrent builds of the
same mint, different mints still build in parallel, and _sweep
cleans stale lock files without touching a held one. No live
network: _res and _safety are fakes.
"""
import json
import os
import struct
import sys
import tempfile
import threading
import time

_TMP_HOME = tempfile.mkdtemp(prefix="dossier-home-")
os.environ["FOREX_AGENT_HOME"] = _TMP_HOME

REPO = "/home/hatch/workspace/Forex-agent/Forex-agent-main"
sys.path.insert(0, os.path.join(REPO, "daemon", "launch_scanner"))
sys.path.insert(0, REPO)

from daemon.launch_scanner import dossiers as D
from daemon.launch_scanner.reserves import parse_bonding_curve

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# ---- fakes -------------------------------------------------------
class FakeRes:
    def __init__(self):
        self.curve_price_calls = 0
        self.get_account_calls = 0

    def curve_price_usd(self, mint):
        self.curve_price_calls += 1
        return 123.45, "fake"

    def derive_curve_pda(self, mint):
        return "CurvePDA" + mint[:8]

    def get_account(self, pda):
        self.get_account_calls += 1
        data = bytearray(64)
        struct.pack_into("<Q", data, 8, 1_000_000_000)   # v_token
        struct.pack_into("<Q", data, 16, 5_000_000_000)  # v_sol
        data[48] = 0
        return "owner", bytes(data)

    def parse_bonding_curve(self, data):
        return parse_bonding_curve(data)


class FakeSafety:
    def __init__(self):
        self.calls = 0
        self.delay = 0.0

    def check_token(self, chain, mint, timeout=10):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return {"unknown": False, "score": 90, "source": "rugcheck"}


RES = FakeRes()
SAF = FakeSafety()
D._res = RES
D._safety = SAF
D._intel_fields = lambda deployer: {}


def _item(mint, buy=2.0, mcap=50.0):
    return {"mint": mint, "symbol": "TEST", "created_ts": time.time(),
            "extra": {"trader": "Trader11111111111111111111111111111",
                      "bonding_curve": "Curve1111111111111111111111111111",
                      "initial_buy_sol": buy, "market_cap_sol": mcap}}


M1 = "MintOne11111111111111111111111111111111111pump"
M2 = "MintTwo22222222222222222222222222222222222pump"
M3 = "MintTre33333333333333333333333333333333333pump"

# ---- birth filter unchanged --------------------------------------
check("birth filter: small buy + small mcap -> False",
      D._birth_filter(_item(M1, buy=0.5, mcap=10.0)) is False)
check("birth filter: big buy -> True",
      D._birth_filter(_item(M1, buy=1.5, mcap=10.0)) is True)
check("birth filter: big mcap -> True",
      D._birth_filter(_item(M1, buy=0.1, mcap=35.0)) is True)

# ---- single build: one curve fetch, no curve_price_usd call ------
D._build(_item(M1))
check("single build: curve_price_usd never called",
      RES.curve_price_calls == 0)
check("single build: exactly one curve get_account",
      RES.get_account_calls == 1)
check("single build: safety check ran once", SAF.calls == 1)
d1 = D.read_dossier(M1)
check("single build: dossier readable", isinstance(d1, dict))
check("single build: curve sample has fake reserves",
      d1 is not None and len(d1.get("curve_samples") or []) == 1
      and d1["curve_samples"][0]["v_sol"] == 5_000_000_000
      and d1["curve_samples"][0]["v_token"] == 1_000_000_000)
check("single build: safety report stored",
      d1 is not None and (d1.get("safety") or {}).get("report") is not None
      and d1["safety"]["report"].get("score") == 90)

# ---- repeat build: re-check skips a second build ------------------
D._build(_item(M1))
check("repeat build: safety still ran once total", SAF.calls == 1)
check("repeat build: get_account still once total",
      RES.get_account_calls == 1)

# ---- concurrent builds, same mint: only one builds ---------------
SAF.delay = 0.3
_res_calls_before = RES.get_account_calls
_saf_calls_before = SAF.calls
_bar = threading.Barrier(2)
_t1 = threading.Thread(target=lambda: (_bar.wait(),
                                       D._build(_item(M2))))
_t2 = threading.Thread(target=lambda: (_bar.wait(),
                                       D._build(_item(M2))))
_t1.start()
_t2.start()
_t1.join()
_t2.join()
SAF.delay = 0.0
check("concurrent same mint: safety ran exactly once more",
      SAF.calls == _saf_calls_before + 1)
check("concurrent same mint: get_account ran exactly once more",
      RES.get_account_calls == _res_calls_before + 1)
check("concurrent same mint: one dossier file",
      D.read_dossier(M2) is not None)

# ---- concurrent builds, different mints: both build --------------
_saf_calls_before = SAF.calls
_bar2 = threading.Barrier(2)
_u1 = threading.Thread(target=lambda: (_bar2.wait(),
                                       D._build(_item(M3))))
_u2 = threading.Thread(target=lambda: (_bar2.wait(),
                                       D._build(_item(M1 + "x"))))
_u1.start()
_u2.start()
_u1.join()
_u2.join()
check("concurrent different mints: both built",
      SAF.calls == _saf_calls_before + 2)
check("concurrent different mints: both dossiers readable",
      D.read_dossier(M3) is not None
      and D.read_dossier(M1 + "x") is not None)

# ---- _sweep cleans stale locks, keeps a held one ------------------
import fcntl
stale_mint = "StaleMint4444444444444444444444444444444pump"
stale_lock = os.path.join(D.DOSSIER_DIR, stale_mint + ".lock")
os.makedirs(D.DOSSIER_DIR, exist_ok=True)
with open(stale_lock, "w") as f:
    f.write("")
D._last_sweep = 0.0
D._sweep(time.time())
check("sweep: stale lock file removed", not os.path.exists(stale_lock))

held_mint = "HeldMint55555555555555555555555555555555pump"
held_lock = os.path.join(D.DOSSIER_DIR, held_mint + ".lock")
held_fd = os.open(held_lock, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
D._last_sweep = 0.0
D._sweep(time.time())
check("sweep: held lock file kept", os.path.exists(held_lock))
fcntl.flock(held_fd, fcntl.LOCK_UN)
os.close(held_fd)

print()
if fails:
    print("%d FAILURES: %s" % (len(fails), fails))
    sys.exit(1)
print("ALL DOSSIER TESTS PASSED")
