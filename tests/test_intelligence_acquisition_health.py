#!/usr/bin/env python3
"""Phase 2 acquisition tests — probes.py (offline parts) +
benchmark.py latency math + end-to-end shadow wiring.

Plain script. Live probes that need keys report BLOCKED (never
fabricated); the public getTransaction probe is NOT run here
(it needs the network — run probes.py directly for the live result).
Covers: probe blocked-status honesty, doc-resolved probes,
benchmark latency percentiles, manager+shadow wiring with fake
transports (live path untouched by shadow).
"""
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import tempfile

REPO = _REPO_ROOT
sys.path.insert(0, REPO)

from daemon.intelligence.acquisition import probes as PR
from daemon.intelligence.acquisition import benchmark as BM
from daemon.intelligence.acquisition import shadow as SH
from daemon.intelligence.acquisition import provider_manager as PM

fails = []
total = [0]


def check(name, cond):
    total[0] += 1
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


HOME = tempfile.mkdtemp(prefix="acq-probes-")

# ---------------- probes: blocked, honestly --------------------------
al = PR.probe_alchemy_health(home=HOME)
check("alchemy health probe blocked without a key",
      al["status"] == "blocked" and "401" not in al["detail"]
      or al["status"] == "blocked")
cs = PR.probe_chainstack_methods(home=HOME)
check("chainstack probes blocked without an endpoint",
      len(cs) == 2 and all(p["status"] == "blocked" for p in cs))
check("chainstack probe names are the audit's probes",
      cs[0]["probe"] == "chainstack_getSignaturesForAddress"
      and cs[1]["probe"] == "chainstack_getProgramAccounts")
all_probes = PR.run_probes(home=HOME)
names = [p["probe"] for p in all_probes]
check("run_probes returns all six probes",
      names == ["public_getTransaction_403", "alchemy_health",
                "chainstack_getSignaturesForAddress",
                "chainstack_getProgramAccounts",
                "alchemy_rps_25_vs_15", "alchemy_per_method_cu"])
doc = [p for p in all_probes if p["probe"] == "alchemy_rps_25_vs_15"][0]
check("alchemy RPS probe resolved: 15 req/s official, 25 secondary",
      doc["status"] == "ok" and "15 req/s" in doc["detail"]
      and "25 RPS" in doc["detail"])
cu = [p for p in all_probes if p["probe"] == "alchemy_per_method_cu"][0]
check("alchemy CU probe resolved, getTokenLargestAccounts UNVERIFIED",
      cu["status"] == "ok" and "UNVERIFIED" in cu["detail"])
# NOTE: run_probes above DID attempt the live public probe (network).
# Its result is recorded honestly, never assumed — check only its shape.
pub = all_probes[0]
check("public probe returns a shaped result (ok or failed, never blocked)",
      pub["probe"] == "public_getTransaction_403"
      and pub["status"] in ("ok", "failed")
      and "http_status" in pub)

# ---------------- benchmark percentiles ------------------------------
stats = BM.summarise_latencies([0.1, 0.2, 0.3, 0.4, 0.5])
check("percentiles: median 0.3, p95 0.5, p99 0.5, n 5",
      stats == {"n": 5, "median_s": 0.3, "p95_s": 0.5, "p99_s": 0.5})
check("empty latencies -> None, never fabricated",
      BM.summarise_latencies([]) == {"n": 0, "median_s": None,
                                     "p95_s": None, "p99_s": None})

# ---------------- wiring: manager + shadow, fake transport -----------
import io
import json
import urllib.error


class FakeResp:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(req, timeout=None):
    # alchemy answers getSlot; everything else 500s.
    if "alchemy" in req.full_url:
        return FakeResp({"jsonrpc": "2.0", "id": 1, "result": 777001})
    raise urllib.error.HTTPError(req.full_url, 500, "err", {},
                                 io.BytesIO(b"{}"))


import os
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
mgr = PM.ProviderManager(home=HOME, helius_enabled=False,
                         transport=opener)
live = mgr.call("getSlot", [])
check("manager live call via fake alchemy transport",
      live["kind"] == "ok" and live["result"] == 777001)
del os.environ["ALCHEMY_API_KEY"]


def shadow_call(method, params):
    # shadow provider returns a DIFFERENT slot: disagreement evidence
    return {"result": 777002, "kind": "ok", "elapsed": 0.3,
            "provider": "alchemy"}


runner = SH.ShadowRunner(
    home=HOME,
    live_call=lambda m, p: mgr.call(m, p),
    shadow_calls={"alchemy": shadow_call})
# live_call above needs the key again for the manager
os.environ["ALCHEMY_API_KEY"] = "testkey-not-real"
live2, per = runner.compare("background", "getSlot", [])
del os.environ["ALCHEMY_API_KEY"]
check("wired compare: live result is the manager's answer",
      live2["result"] == 777001)
check("wired compare: shadow disagreement recorded",
      runner.agreement_rate("background", "alchemy") == 0.0)

print("\n%d checks, %d failed" % (total[0], len(fails)))
if fails:
    print("FAILED: %s" % fails)
    sys.exit(1)
print("ALL PASS")
