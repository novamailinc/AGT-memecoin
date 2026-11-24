#!/usr/bin/env python3
"""Derive pump.fun bonding-curve PDA for both mints and probe its tx span (asc)."""
import hashlib, json, os, urllib.request

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58dec(s):
    n = 0
    for c in s:
        n = n * 58 + B58.index(c)
    b = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + b
def b58enc(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out

P = 2**255 - 19
D = (-121665 * pow(121666, P - 2, P)) % P
def on_curve(b):
    y = int.from_bytes(b, "little") & (P)
    if y >= P:
        return False
    y2 = y * y % P
    x2 = (y2 - 1) * pow((D * y2 + 1) % P, P - 2, P) % P
    # x exists iff x2 is quadratic residue (or 0)
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1

def find_pda(seeds, program):
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + program + b"ProgramDerivedAddress").digest()
        if not on_curve(h):
            return b58enc(h), bump
    raise RuntimeError("no pda")

HOME = os.path.expanduser("~")
def load_key():
    for name in ("api_key_discovery", "api_key"):
        p = os.path.join(HOME, ".forex-agent/helius", name)
        if os.path.exists(p):
            return open(p).read().strip()
KEY = load_key()
RPC = f"https://mainnet.helius-rpc.com/?api-key={KEY}"
def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("result")

PROG = b58dec("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
for name, mint in [("KIMCHI", "7thgKgtK1rp1UtLpWUZEuu9vakedTMoZzuyps6RGpump"),
                   ("KWH", "5hyyjwZqJgnPZorMdb7X4r2J1dT3VujKVkLD8Ptbpump")]:
    pda, bump = find_pda([b"bonding-curve", b58dec(mint)], PROG)
    print(f"{name} bonding-curve PDA: {pda} (bump {bump})")
    res = rpc("getTransactionsForAddress", [pda, {"sortOrder": "asc", "limit": 3}])
    data = (res or {}).get("data") or []
    for d in data:
        print("   first:", d.get("blockTime"), d.get("signature"))
    res2 = rpc("getTransactionsForAddress", [pda, {"sortOrder": "desc", "limit": 3}])
    for d in (res2 or {}).get("data") or []:
        print("   last:", d.get("blockTime"), d.get("signature"))
