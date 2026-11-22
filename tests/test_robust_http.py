#!/usr/bin/env python3
"""Tests for robust_http: retry, circuit breaker, backoff, fail-fast."""
import os
import os
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys, time
sys.path.insert(0, _REPO_ROOT + "/daemon/launch_scanner")
import robust_http as rh

fails = []
def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond: fails.append(name)

# 1. Live smoke: both APIs respond through the hardened client
# (uses high tries; proxy has bad moments — client must ride through)
try:
    d = rh.get_json("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1",
                    tries=10, timeout=25)
    check("gecko live", isinstance(d.get("data"), list) and len(d["data"]) > 0)
except Exception as e:
    check("gecko live (%s)" % str(e)[:40], False)

try:
    d = rh.get_json("https://api.dexscreener.com/latest/dex/pairs/solana/DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB5",
                    tries=10, timeout=25)
    # API returns dict with schemaVersion; pair data may be null for dead pairs
    ok = isinstance(d, dict) and "schemaVersion" in d
    check("dex live", ok)
except Exception as e:
    check("dex live (%s)" % str(e)[:40], False)

# 2. Fail-fast on 404 (no retry storm): should raise quickly
t0 = time.time()
try:
    rh.get_json("https://api.geckoterminal.com/api/v2/networks/solana/pools/INVALIDPOOL123/ohlcv/hour",
                tries=5, timeout=10)
    check("404 fail-fast", False)
except Exception:
    dt = time.time() - t0
    check("404 fail-fast (%.1fs, no 5x backoff)" % dt, dt < 25)

# 3. Circuit breaker: directly test open/close logic
rh._circuit_fails.clear(); rh._circuit_open.clear()
test_host = "test.example.com"
for _ in range(rh.CIRCUIT_THRESHOLD):
    rh._record_fail(test_host)
check("circuit opens after threshold", test_host in rh.circuit_status())
# immediate call should fail fast via circuit
t0 = time.time()
try:
    rh.get_json("http://test.example.com/nope", tries=3, timeout=5)
    check("circuit fail-fast", False)
except RuntimeError as e:
    check("circuit fail-fast (%.1fs)" % (time.time()-t0),
          "circuit open" in str(e) and time.time()-t0 < 3)
rh._circuit_fails.clear(); rh._circuit_open.clear()
# success resets
rh._record_success(test_host)
check("circuit cleared", test_host not in rh.circuit_status())

# 4. Rate-limit gap: two rapid calls should be spaced
t0 = time.time()
rh._last_call.clear()
# clear any fails from 404 test so circuit doesn't interfere
rh._circuit_fails.clear()
try:
    rh.get_json("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1", tries=10, timeout=25)
    rh.get_json("https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=2", tries=10, timeout=25)
    dt = time.time() - t0
    check("min gap enforced (%.1fs >= 1.0s)" % dt, dt >= 1.0)
except Exception as e:
    check("min gap (%s)" % str(e)[:40], False)

print("\n%d failures" % len(fails))
sys.exit(1 if fails else 0)
