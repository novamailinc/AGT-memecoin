#!/usr/bin/env python3
"""Probe Helius getTransactionsForAddress shapes."""
import json, os, urllib.request

HOME = os.path.expanduser("~")
def load_key():
    for name in ("api_key_discovery", "api_key"):
        p = os.path.join(HOME, ".forex-agent/helius", name)
        if os.path.exists(p):
            return open(p).read().strip()
    raise SystemExit("no key")
KEY = load_key()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code, "body": e.read().decode()[:1000]}

MINT = "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump"
# graduation window: 1792540200 +- 45min
T0, T1 = 1792540200 - 2700, 1792540200 + 2700

attempts = [
    ("v1 gteTime/lteTime", [MINT, {"sortOrder": "asc", "limit": 5, "gteTime": T0, "lteTime": T1}]),
    ("v2 fromTime/toTime", [MINT, {"sortOrder": "asc", "limit": 5, "fromTime": T0, "toTime": T1}]),
    ("v3 time-range keys", [MINT, {"sortOrder": "asc", "limit": 5, "timeRange": {"gte": T0, "lte": T1}}]),
    ("v4 plain asc", [MINT, {"sortOrder": "asc", "limit": 3}]),
]
for name, params in attempts:
    print(f"\n### {name}")
    res = rpc("getTransactionsForAddress", params)
    s = json.dumps(res)[:1200]
    print(s)
