#!/usr/bin/env python3
"""Backward-compat shim: old nadir_http.http_json -> robust_http.get_json."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "..", "daemon", "launch_scanner"))
from robust_http import get_json, post_json, circuit_status  # noqa

def http_json(url, timeout=20, tries=5, backoff_base=2.0, headers=None):
    return get_json(url, timeout=timeout, tries=tries,
                    headers=headers, backoff_base=backoff_base)

if __name__ == "__main__":
    for name, url in [
        ("gecko", "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1"),
        ("dex", "https://api.dexscreener.com/latest/dex/pairs/solana/DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB5"),
    ]:
        try:
            d = http_json(url)
            n = len(d.get("data", d) if isinstance(d, dict) else d)
            print("%s: OK (%s items)" % (name, n))
        except Exception as e:
            print("%s: FAIL %s" % (name, str(e)[:60]))
    print("circuits:", circuit_status())
