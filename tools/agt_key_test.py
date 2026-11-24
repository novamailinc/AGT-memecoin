#!/usr/bin/env python3
"""AGT phone key test: prove the Alchemy + Chainstack API keys with real values.

Run as an AGT deployment with secrets injection (deploy payload "secrets":
["alchemy-api-key", "chainstack-api-key"]). The phone injects them as
ALCHEMY_API_KEY / CHAINSTACK_API_KEY env vars. This script NEVER prints a
key value — only the HTTP status and the getHealth result per provider.

Usage on the phone (deployment start command):
    python3 key_test.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

PROVIDERS = [
    ("alchemy", "ALCHEMY_API_KEY",
     "https://solana-mainnet.g.alchemy.com/v2/{}"),
    ("chainstack", "CHAINSTACK_API_KEY",
     "https://solana-mainnet.core.chainstack.com/{}"),
]


def check(name, url_template, key):
    url = url_template.format(key)
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": "getHealth", "params": []}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:200]
    except Exception as exc:  # noqa: BLE001 - report, do not crash the run
        return None, f"{type(exc).__name__}: {str(exc)[:150]}"


def main():
    ok = True
    for name, env_var, url_template in PROVIDERS:
        key = os.environ.get(env_var, "")
        if not key:
            print(f"{name}: MISSING {env_var} (not injected from vault)")
            ok = False
            continue
        status, body = check(name, url_template, key)
        print(f"{name}: HTTP {status} {body}")
        if status != 200:
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
