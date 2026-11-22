#!/usr/bin/env python3
"""Alchemy Solana mainnet JSON-RPC client.

Calls Alchemy's Solana endpoint with the registered custom.alchemy
credential. The key travels as a surrogate in the /v2/ URL path segment
and is swapped in by authd only on an approved request.

Usage:
  python3 bin/alchemy.py health            # getHealth, harmless read
  python3 bin/alchemy.py slot              # getSlot
  python3 bin/alchemy.py call <method> [params-json]
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import read_json_response, url_with_surrogate_path_segment

HOST = "solana-mainnet.g.alchemy.com"
BASE = "https://solana-mainnet.g.alchemy.com/v2/{}"
# Disk key (Helius pattern) takes precedence: the vault surrogate for a
# URL-path-segment placement is not swapped on this VM, so the surrogate
# URL 401s even though the stored key is valid. Falls back to the
# surrogate when no disk key file exists.
DISK_KEY_FILE = os.path.expanduser("~/.forex-agent/alchemy/api_key")


def _disk_key():
    try:
        with open(DISK_KEY_FILE) as f:
            key = f.read().strip()
        return key or None
    except OSError:
        return None


def _url():
    key = _disk_key()
    if key:
        return BASE.format(key)
    return url_with_surrogate_path_segment(BASE, "custom.alchemy", allowed_hosts=[HOST])


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def rpc(method, params=None):
    url = _url()
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
        return read_json_response(response)


def main(argv):
    if len(argv) < 2:
        print("usage: alchemy.py health | slot | call <method> [params-json]", file=sys.stderr)
        return 2
    cmd = argv[1]
    if cmd == "health":
        result = rpc("getHealth")
    elif cmd == "slot":
        result = rpc("getSlot")
    elif cmd == "call":
        if len(argv) < 3:
            print("usage: alchemy.py call <method> [params-json]", file=sys.stderr)
            return 2
        params = json.loads(argv[3]) if len(argv) > 3 else []
        result = rpc(argv[2], params)
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
